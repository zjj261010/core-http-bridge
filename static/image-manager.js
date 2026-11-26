(() => {
  'use strict';
  const P=Platform,{el,button,field,input,select,checkbox,table,api}=P;
  const app=document.getElementById('app');let images=[],manifests={},jobTimer;
  P.navigation('target-admin.html');
  const uid=prefix=>prefix+'-'+(crypto.randomUUID?crypto.randomUUID():[...crypto.getRandomValues(new Uint8Array(16))].map(b=>b.toString(16).padStart(2,'0')).join(''));  // randomUUID 仅安全上下文(HTTPS/localhost)可用, HTTP+IP 访问走 getRandomValues 回退
  const stateName=value=>({applied:'已生效',pending:'已保存待生效',unconfigured:'未配置服务',incomplete:'已保存待完善',failed:'生效失败'})[value]||value;
  const importStatus={queued:'排队中',running:'处理中',succeeded:'已完成',partial:'部分完成',failed:'失败',interrupted:'已中断'};
  const importStage={queued:'等待处理',unpacking:'解包',validating:'校验归档',loading:'准备加载',image:'加载镜像',services:'服务配置',manifest:'清单'};
  const guarded=(message,fn)=>async()=>{try{await fn();}catch(error){P.localMessage(message,error.message,true);}};
  const noticeArea=()=>el('div',{'aria-live':'polite'});
  function imageSection(title, contents, actions=null, variant=''){
    return el('section',{class:'image-dialog-section '+variant},
      el('div',{class:'image-dialog-section-title'},el('h3',{},title),actions),
      el('div',{class:'image-dialog-section-body'},contents));
  }
  function modeGroup(title,...contents){
    return el('section',{class:'mode-group'},el('h4',{},title),contents);
  }
  function blockingSessions(sessions = []){
    const states={2:'配置中',3:'启动中',4:'运行中',5:'停止中'};
    return sessions.length ? table(['CORE 会话','状态','操作'],sessions.map(session=>[
      session.id,states[session.state]||'状态待确认',
      el('a',{href:'/static/topologies.html?session='+encodeURIComponent(session.id),target:'_blank',rel:'noopener'},'查看会话')
    ])) : null;
  }
  async function load(){
    const data=await api('/targets');images=data.images;manifests=data.manifests;
    app.replaceChildren(el('header',{class:'page-heading'},el('div',{},el('h1',{},'镜像管理'),el('p',{class:'muted'},'声明镜像提供的完整服务，在模板与节点中选择实际启用项。')),button('导入',importImage,true)),
      el('div',{id:'imageInventory'},imageTable(images)),el('section',{id:'jobs',class:'surface'}));
    await loadJobs();
  }
  function imageTable(items){
    return table(['镜像 / 标签','本地大小','导入时间','操作'],items.map(image=>[
      image.ref,P.bytes(image.size_bytes),image.import_time||'未记录',
      el('div',{class:'actions'},button('详情',()=>openImage(image.ref),true),button('导出',()=>exportImage(image.ref)),
        button('删除镜像',()=>removeImage(image.ref)))
    ]));
  }
  async function removeImage(ref){if(!confirm('删除本地镜像 '+ref+'？已被容器或部署使用的镜像会被拒绝。'))return;await api('/targets/image/delete','POST',{ref});await load();}
  async function loadJobs(){
    clearTimeout(jobTimer);const area=document.getElementById('jobs');if(!area)return;
    const [jobs,exports]=await Promise.all([api('/jobs'),api('/image-services/exports')]);if(!area.isConnected)return;
    const imports=table(['时间','状态 / 阶段','结果','操作'],jobs.slice().reverse().map(job=>{
      const refs=Object.keys(job.result?.pieces?.image?.receipt?.images||{});
      return [
        job.created_at,(importStatus[job.status]||job.status)+' · '+(importStage[job.stage]||job.stage||'等待处理'),
        el('div',{},job.message||'',Object.entries(job.result?.pieces||{}).map(([name,item])=>el('p',{},name+'：'+item.detail))),
        el('div',{class:'actions'},
          ['failed','partial','interrupted'].includes(job.status)?button('续办',()=>resumeImport(job)):
            button('查看结果',()=>P.modal('任务结果',P.fields(job.result||{}),[button('刷新',loadJobs)])),
          refs.length === 1 ? button('打开镜像详情',()=>openImage(refs[0])) : null)
      ];
    }));
    const exported=table(['镜像','状态 / 阶段','结果','操作'],exports.slice().reverse().map(job=>[
      job.image_ref,job.state+' / '+(job.stage||'等待处理'),job.message||'',
      job.state==='succeeded'?el('a',{href:job.download},'下载完整镜像包'):['failed','interrupted'].includes(job.state)?button('重新导出',()=>exportImage(job.image_ref,null,'full')):'后台处理中'
    ]));
    area.replaceChildren(el('h2',{},'导入任务'),imports,el('h2',{},'完整包导出任务'),exported);
    const importing=jobs.some(job=>['queued','running'].includes(job.status));
    if(area.dataset.importing==='true' && !importing){
      const data=await api('/targets');
      if(area.isConnected){images=data.images;document.getElementById('imageInventory').replaceChildren(imageTable(images));}
    }
    area.dataset.importing=String(importing);
    if(importing||exports.some(job=>['queued','running'].includes(job.state)))jobTimer=setTimeout(()=>loadJobs().catch(error=>P.notice(error.message,true)),2000);
  }

  async function resumeImport(job){const record=await api('/jobs/'+job.job_id),message=noticeArea();const imagePath=input('',()=>{});const body=el('div',{},el('p',{},'沿用已接收文件和已完成结果。如镜像文件已移动，可重新指定路径；配置内容在镜像详情中导入和修改。'),field('新的镜像路径（可留空）',imagePath),message);const modal=P.modal('续办导入',body);modal.footer.append(button('取消',modal.close),button('继续导入',guarded(message,async()=>{const patch=imagePath.value?{image_path:imagePath.value,defer_services:true}:{defer_services:true};await api('/jobs/'+job.job_id+'/resume','POST',patch);modal.close();await loadJobs();}),true));}
  async function exportImage(ref,catalog=null,initialScope='config'){const message=noticeArea(),scope=select([['config','配置与脚本'],['full','完整镜像包']], initialScope,()=>{});const modal=P.modal('导出',el('div',{},field('导出范围',scope),el('p',{class:'muted'},'配置与脚本包含全部附件；完整包另含镜像本体，后台打包后下载。'),message));modal.footer.append(button('取消',modal.close),button('开始导出',guarded(message,async()=>{if(scope.value==='config'){if(catalog)await P.downloadConfig(catalog);else {const response=await fetch('/image-services/export?ref='+encodeURIComponent(ref));if(!response.ok){const data=await response.json();throw Error(P.errorText(data.detail));}P.download(await response.blob(),'image-config.zip');}P.localMessage(message,'配置与脚本已生成。');}else {let job=await api('/image-services/export/full','POST',{ref});P.localMessage(message,'完整包正在后台生成。');const poll=async()=>{if(!modal.dialog.open)return;try{job=await api('/image-services/exports/'+job.job_id);P.localMessage(message,job.message||'处理阶段：'+job.stage);if(job.state==='succeeded'){message.replaceChildren(el('a',{href:job.download},'下载完整镜像包（'+Math.round(job.size/1024/1024)+' MiB）'));}else if(job.state!=='failed'&&job.state!=='interrupted')setTimeout(poll,2000);}catch(error){P.localMessage(message,error.message,true);}};setTimeout(poll,1000);}}),true));}
  async function openImage(ref){
    let baseline=await api('/image-services/detail?ref='+encodeURIComponent(ref)),work=P.clone(baseline),dirty=false;
    const message=noticeArea(),body=el('div'),modal=P.modal('镜像详情',body,[],()=>{});
    modal.dialog.classList.add('image-dialog');
    const changed=()=>{dirty=true;};
    let draft=null;
    const draftDirty=()=>draft && JSON.stringify(draft.value)!==draft.initial;
    modal.guard(()=>dirty || !!draftDirty());
    const close=modal.close;
    function render(){
      draft=null;
      const metadata=P.fields({镜像:ref,镜像标识:work.image_id,平台:work.info.platform,本地大小:P.bytes(work.info.size),工作目录:work.info.workdir,启动入口:work.info.entrypoint,启动命令:work.info.command,标签:work.info.tags,配置状态:stateName(work.state),处理阶段:work.failed_stage||work.stage||'—'});
      const serviceActions=el('div',{class:'actions'},
        button('导入清单',()=>chooseCatalog(work,acceptPreview)),button('导出当前配置与脚本',()=>P.downloadConfig(work)),
        button('新增服务',()=>editService(null),true));
      const serviceRows=work.services.map(service=>[
        el('div',{class:JSON.stringify(service)!==JSON.stringify(baseline.services.find(s=>s.id===service.id))?'changed-row':''},service.name||'未命名服务'),
        service.software||'—',service.description||'—',String(service.modes.length),
        el('div',{class:'actions'},serviceDetailsButton(service),button('编辑',()=>editService(service)),button('移除声明',()=>deleteDeclaration(service)))
      ]);
      const imports=(work.imports||[]).map(item=>button('预览导入 '+item.created_at,()=>{
        const rows=item.catalog.services.map(incoming=>{
          const existing=work.services.find(s=>s.id===incoming.id||s.name.toLocaleLowerCase()===incoming.name.toLocaleLowerCase());
          return {incoming,existing,match_id:existing?.id,action:existing?'skip':'add'};
        });
        previewImport({rows,legacy_sources:item.catalog.legacy_sources,legacy_templates:item.catalog.legacy_templates},work,acceptPreview,body);
      }));
      P.replace(body,message,
        imageSection('基本信息',[metadata,work.message?el('p',{class:'notice'},work.message):null,blockingSessions(work.activation_blockers),
          work.migration_note?el('p',{class:'muted'},work.migration_note):null],null,'image-metadata'),
        imageSection('服务清单',[table(['服务','软件版本','用途','启动模式','操作'],serviceRows),
          el('p',{class:'muted'},'清单包含镜像的全部服务。暂时不运行时在模板或节点取消启用；移除声明不会卸载镜像软件。')],serviceActions),
        imports.length?imageSection('已接收的导入记录',el('div',{class:'actions'},imports)):null);
      modal.footer.replaceChildren(button('取消',close),button('确定',guarded(message,review),true));
    }
    function serviceDetailsButton(service){const action=button('详情',()=>{if(draftDirty()&&!confirm('放弃当前服务表单尚未暂存的修改？'))return;if(draft)render();modesDialog(service,work,updated=>{work.services[work.services.findIndex(s=>s.id===service.id)]=updated;changed();render();});});action.dataset.returnId=service.id;return action;}
    function acceptPreview(value){work=value;changed();render();P.localMessage(message,'导入内容已合入本次编辑，尚未保存或注册。');}
    function editService(service){
      if(draftDirty()&&!confirm('放弃当前服务表单尚未暂存的修改？'))return;
      render();
      const copy=P.clone(service||{id:uid('service'),name:'',software:'',description:'',ports:[],modes:[]});
      const name=input(copy.name,value=>copy.name=value);
      const form=imageSection(service?'编辑服务 · '+service.name:'新增服务',[
        el('div',{class:'form-grid'},field('服务名称',name),field('软件版本',input(copy.software,value=>copy.software=value))),
        field('用途说明',input(copy.description,value=>copy.description=value)),
        modeGroup('监听端口',P.ports(copy.ports)),
        el('div',{class:'actions'},button('取消编辑',render),button('暂存此服务',()=>{
          if(!copy.name.trim()){P.localMessage(message,'请填写服务名称',true);message.scrollIntoView({block:'nearest'});return;}
          if(work.services.some(s=>s.id!==copy.id&&s.name.trim().toLocaleLowerCase()===copy.name.trim().toLocaleLowerCase())){
            P.localMessage(message,'服务名称重复',true);message.scrollIntoView({block:'nearest'});return;
          }
          const index=work.services.findIndex(s=>s.id===copy.id);
          if(index<0)work.services.push(copy);else work.services[index]=copy;
          changed();render();
        },true))
      ],null,'mode-editor');
      draft={value:copy,initial:JSON.stringify(copy)};
      body.append(form);
      form.scrollIntoView({block:'start'});
      name.focus({preventScroll:true});
    }
    function deleteDeclaration(service){const area=el('section',{class:'surface'},el('h3',{},'移除 '+service.name+' 的声明？'),el('p',{},'会移除本清单中的该服务及其模式，不卸载软件，不直接停止已有节点。旧模板和历史实现继续保留。若只是本次不运行，请保留声明并在模板或节点取消启用。'),el('p',{class:'muted'},'相关模板修订：'+(work.references?.revisions?.length||0)+'；最终确认时统一核对引用。'),el('div',{class:'actions'},button('保留',render),button('确认移除声明',()=>{work.services=work.services.filter(s=>s.id!==service.id);changed();render();})));body.append(area);area.scrollIntoView({block:'nearest'});}
    async function review(){
      if(draft){P.localMessage(message,'请先暂存服务修改或取消当前服务编辑。',true);return;}
      const result=await api('/image-services/'+work.catalog_id+'/validate','POST',{services:work.services});
      const status=await api('/image-services/activation-status').catch(()=>null);
      const blockers=status?.blockers||[];
      const acknowledgment=checkbox(false,()=>{
        apply.disabled=!acknowledgment.querySelector('input').checked||!status||blockers.length>0||result.issues.length>0;
      },'我确认保存并向 CORE 注册此配置。若后续需要重启 CORE，全部会话将受影响。');
      const guidance=!status?'CORE 状态暂不可用，可以先保存配置。':blockers.length?
        '请先停止下列活动会话。也可以先保存配置，稍后生效。':'独立修订可以直接注册，已停止的会话可以保留。';
      P.replace(body,message,imageSection('最后确认', [
        P.fields({声明服务:work.services.map(s=>s.name),新增服务:work.services.filter(s=>!baseline.services.some(old=>old.id===s.id)).map(s=>s.name),移除声明:baseline.services.filter(s=>!work.services.some(now=>now.id===s.id)).map(s=>s.name),受引用模板修订:work.references?.revisions||[]}),
        el('div',{class:'notice'},guidance),blockingSessions(blockers),
        result.issues.length?P.fields({待完善项:result.issues}):null,acknowledgment]));
      modal.content.scrollTop=0;
      const save=async action=>{const record=await api('/image-services/'+work.catalog_id+'/commit','POST',{...work,base_revision:work.revision,action,acknowledge:acknowledgment.querySelector('input').checked,request_id:uid('request')});work={...work,...record};baseline=P.clone(work);dirty=false;render();P.localMessage(message,stateName(record.state)+'。'+(record.message||''),record.state==='failed');await load();};
      const apply=button('确认保存并生效',guarded(message,()=>save('apply')),true);apply.disabled=true;
      modal.footer.replaceChildren(button('返回修改',render),button('仅保存，稍后生效',guarded(message,()=>save('save'))),apply);
    }
    render();
  }
  function modesDialog(service,catalog,saved){
    const work=P.clone(service),initial=JSON.stringify(service),message=noticeArea(),body=el('div'),modal=P.modal(service.name+' · 启动模式',body);
    modal.dialog.classList.add('image-dialog');
    let editing=null;
    const moved=[];
    modal.guard(()=>JSON.stringify(work)!==initial||moved.length>0,'放弃当前启动模式尚未暂存的修改？');
    function validateLocal(){
      const names=work.modes.map(mode=>mode.name.trim().toLocaleLowerCase());
      const validNames=names.every(Boolean)&&new Set(names).size===names.length;
      const issues=validNames?[]:['模式名称不能为空或重复'];
      for(const mode of work.modes)if(mode.strategy==='script'&&!mode.files.some(file=>file.role==='startup'))issues.push(mode.name+' 缺少启动脚本');
      P.localMessage(message,issues.join('；'),!!issues.length);
      return validNames;
    }
    function render(focusEditor=false){
      const add=button('新增模式',()=>{
        const mode={id:uid('mode'),name:'新模式',strategy:'script',interpreter:'sh',workdir:'/',args:'',env:[],ports:null,depends_on:[],default:!work.modes.length,files:[],cve:[],vulnerable:null};
        work.modes.push(mode);editing=mode.id;render(true);
      },true);
      // 漏洞标注（纯声明元数据，平台不验证实际是否存在该漏洞）
      const vulnTag=mode=>{
        if(mode.vulnerable===true)return el('span',{class:'cve-tag cve-vuln'},...(mode.cve||[]).length?mode.cve:['有']);
        if(mode.vulnerable===false)return el('span',{class:'cve-tag cve-baseline'},'无');
        return el('span',{class:'muted'},'未知');
      };
      const rows=work.modes.map(mode=>[
        el('span',{class:mode.id===editing?'mode-row-active':''},mode.name),
        ({script:'脚本',manual:'手动',legacy:'原有实现附件'})[mode.strategy],
        vulnTag(mode),
        el('div',{},mode.files.filter(file=>file.role==='startup').map(file=>el('div',{},file.name))),
        checkbox(mode.default,checked=>{work.modes.forEach(item=>item.default=checked&&item.id===mode.id);render();}),
        el('div',{class:'actions'},
          button('编辑 / 脚本',()=>{editing=mode.id;render(true);}),
          button('删除模式',()=>{work.modes=work.modes.filter(item=>item.id!==mode.id);editing=null;render();}))
      ]);
      const mode=work.modes.find(item=>item.id===editing);
      const form=mode?editor(mode):null;
      P.replace(body,message,imageSection('模式列表',[
        el('p',{class:'muted'},'节点每次只选择此服务的一种启动模式。'),
        table(['模式','启动方式','漏洞标注','启动脚本','默认','操作'],rows)
      ],add),form,moveEditor());
      validateLocal();
      if(focusEditor && form){
        form.scrollIntoView({block:'start'});
        form.querySelector('input').focus({preventScroll:true});
      }
    }
    function moveEditor(){const candidates=catalog.services.filter(s=>s.id!==service.id).flatMap(s=>s.modes.filter(m=>!moved.some(item=>item.serviceId===s.id&&item.modeId===m.id)).map(m=>({service:s,mode:m})));if(!candidates.length)return null;const chooser=select(candidates.map((item,index)=>[String(index),item.service.name+' / '+item.mode.name]),'0',()=>{});return imageSection('将旧模式合并到此服务',[field('来源服务 / 模式',chooser),el('p',{class:'muted'},'确定后移入模式；来源服务没有剩余模式时移除其空声明。取消本弹窗不会影响来源服务。旧模板和运行记录保持原修订。'),button('暂存移入',()=>{const item=candidates[Number(chooser.value)];const mode=P.clone(item.mode);mode.default=false;work.modes.push(mode);moved.push({serviceId:item.service.id,modeId:item.mode.id});render();})]);}
    function editor(mode){const fileArea=el('div'),environment=mode.env??[];
      function files(){fileArea.replaceChildren(table(['用途','文件','摘要','操作'],mode.files.map((item,index)=>[({startup:'启动',shutdown:'停止',validate:'就绪检查'})[item.role],item.name,item.sha256.slice(0,12),el('div',{class:'actions'},el('a',{href:'/image-services/attachments/'+item.sha256+'?name='+encodeURIComponent(item.name),download:item.name},'导出'),button('上移',()=>{if(index){[mode.files[index-1],mode.files[index]]=[mode.files[index],mode.files[index-1]];files();}}),button('移除',()=>{mode.files.splice(index,1);files();validateLocal();}))])));}
      files();const role=select([['startup','启动'],['shutdown','停止'],['validate','就绪检查']],'startup',()=>{});const upload=el('input',{type:'file',multiple:true,onchange:async event=>{try{for(const file of event.target.files){const attachment=await api('/image-services/attachments?name='+encodeURIComponent(file.name)+'&role='+role.value,'POST',file,true);const index=mode.files.findIndex(f=>f.name===attachment.name&&f.role===attachment.role);if(index<0)mode.files.push(attachment);else mode.files[index]=attachment;}files();validateLocal();}catch(error){P.localMessage(message,error.message,true);}}});
      const override=el('div');
      const effectivePorts=el('div');
      function summarizePorts(){
        const rows=mode.ports??service.ports;
        P.replace(effectivePorts,el('p',{class:'muted'},'生效端口声明 · '+(mode.ports==null?'从服务继承':'此模式覆盖')),
          rows.length?table(['监听地址','协议','端口'],rows.map(row=>[
            row.address||'0.0.0.0',(row.protocol||'tcp').toUpperCase(),
            row.end&&row.end!==row.port?row.port+'–'+row.end:row.port
          ])):el('p',{class:'muted'},'未声明端口'));
      }
      function renderPorts(){
        const overrides=mode.ports!=null;
        P.replace(override,checkbox(overrides,value=>{mode.ports=value?P.clone(service.ports):null;renderPorts();},
          '此模式覆盖服务端口'),overrides?P.ports(mode.ports,summarizePorts):null,effectivePorts);
        summarizePorts();
      }
      renderPorts();
      const dependencies=catalog.services.filter(s=>s.id!==service.id).map(s=>{const existing=(mode.depends_on||[]).find(d=>d.service_id===s.id),dep={service_id:s.id};const options=select([['','任一模式'],...s.modes.map(m=>[m.id,m.name])],existing?.mode_id||'',value=>{const item=mode.depends_on.find(d=>d.service_id===s.id);if(item)item.mode_id=value||null;});options.disabled=!existing;return el('div',{class:'actions'},checkbox(!!existing,checked=>{mode.depends_on??=[];mode.depends_on=mode.depends_on.filter(d=>d.service_id!==s.id);if(checked)mode.depends_on.push(dep);options.disabled=!checked;},s.name),options);});
      const purpose=field('导入用途',role), filePicker=field('选择脚本文件',upload);
      return imageSection('编辑模式 · '+mode.name,[
        el('div',{class:'form-grid'},
          field('模式名称',input(mode.name,value=>{mode.name=value;validateLocal();})),
          field('启动方式',select([['script','按脚本启动'],['manual','仅手动操作'],...(mode.implementation?[['legacy','保留原有实现附件']]:[])],mode.strategy,value=>{mode.strategy=value;validateLocal();})),
          field('执行器',select([['sh','sh'],['bash','bash'],['python3','python3'],['shebang','按脚本声明']],mode.interpreter||'sh',value=>mode.interpreter=value)),
          field('工作目录',input(mode.workdir||'/',value=>mode.workdir=value))),
        // 漏洞标注（纯声明元数据：给人看的提示，平台不验证实际是否有洞，也不参与就绪检查）
        // 顺序: 先定性(漏洞状态)后细节(CVE 编号)——漏洞状态决定"这是不是可攻击环境", CVE 是它的索引细节
        el('div',{class:'form-grid'},
          field('漏洞状态',select([['','未知'],['true','有'],['false','无']],mode.vulnerable===true?'true':mode.vulnerable===false?'false':'',value=>{mode.vulnerable=value===''?null:value==='true';if(value==='true'&&!(mode.cve||[]).length)mode.cve=['CVE-0000-0000'];})),
          field('CVE 标注（逗号分隔，可空）',input((mode.cve||[]).join(', '),value=>{mode.cve=value.split(',').map(s=>s.trim()).filter(Boolean);}))),
        field('传给启动脚本的参数',input(mode.args||'',value=>mode.args=value)),
        modeGroup('脚本文件',fileArea,el('div',{class:'form-grid'},purpose,filePicker),
          el('p',{class:'muted'},'脚本随本次模式暂存，服务在镜像详情最后确认时统一注册。')),
        modeGroup('环境变量',P.keyValues(environment,()=>{mode.env=environment;})),
        modeGroup('端口',override,el('p',{class:'muted'},'端口声明用于组合与就绪检查。脚本实际监听的端口由脚本、启动参数或环境变量决定。')),
        dependencies.length?modeGroup('依赖服务',dependencies):null
      ],null,'mode-editor');
    }
    modal.footer.append(button('取消',modal.close),button('确定并暂存回镜像详情',()=>{if(!validateLocal())return;for(const item of moved){const source=catalog.services.find(s=>s.id===item.serviceId);source.modes=source.modes.filter(m=>m.id!==item.modeId);if(!source.modes.length){catalog.services=catalog.services.filter(s=>s.id!==source.id);for(const target of [...catalog.services,work])for(const mode of target.modes)for(const dep of mode.depends_on||[])if(dep.service_id===source.id)dep.service_id=work.id;}}saved(work);modal.finish();},true));render();
  }
  async function chooseCatalog(catalog,accepted){const file=el('input',{type:'file',accept:'.zip,.json',onchange:async event=>{try{const chosen=event.target.files[0];if(!chosen)return;const preview=await api('/image-services/import/preview?ref='+encodeURIComponent(catalog.image_ref),'POST',chosen,true);const parent=document.querySelector('dialog[open] .dialog-content');previewImport(preview,catalog,accepted,parent);}catch(error){P.notice(error.message,true);}}});file.click();}
  function previewImport(preview,catalog,accepted,parent){const content=el('div'),area=imageSection('导入冲突预览',content),message=noticeArea();const choices=preview.rows.map(row=>{row=P.clone(row);const name=input(row.incoming.name,value=>row.incoming.name=value);const action=select([['skip','跳过'],...(row.match_id?[['replace','替换服务及全部模式']]:[]),['add','新增独立服务']],row.action,value=>row.action=value);return {row,controls:[name,row.existing?.name||'无匹配服务',row.incoming.modes.map(m=>m.name).join('、'),action]};});content.append(el('p',{class:'muted'},'新增同名服务前请修改名称；替换包含该服务全部模式与脚本。合入后仍需最后确定。'),table(['导入名称','现有服务','导入模式','处理方式'],choices.map(x=>x.controls),'preview'),message,el('div',{class:'actions'},button('取消预览',()=>area.remove()),button('合入本次编辑',guarded(message,async()=>{const result=await api('/image-services/import/merge','POST',{catalog,preview:{...preview,rows:choices.map(x=>x.row)}});accepted(result.catalog);area.remove();}),true)));parent.append(area);area.scrollIntoView({block:'nearest'});}
  function importImage(){const message=noticeArea(),source=select([['upload','本机上传'],['path','服务器路径']],'upload',render),kind=select([['bundle','完整交付包（tar.gz）'],['image','镜像归档（tar）']],'bundle',render),path=input('',()=>{}),file=el('input',{type:'file'}),form=el('div');const modal=P.modal('导入',el('div',{},el('div',{class:'form-grid'},field('来源',source),field('内容',kind)),form,message));const autoName=()=>((file.files[0]&&file.files[0].name)||'').replace(/\.(tar\.gz|tgz|tar|gz)$/i,'').replace(/[^a-zA-Z0-9_-]+/g,'-').replace(/^-+|-+$/g,'')||'image-import';
    function render(){form.replaceChildren(source.value==='upload'?field('文件',file):field(kind.value==='bundle'?'服务器包目录':'服务器镜像路径',path),el('p',{class:'muted'},'接收镜像与配置后，在镜像详情中预览服务清单，最后确定才注册。名称自动取自包内清单。'));}render();
    modal.footer.append(button('取消',modal.close),button('导入',guarded(message,async()=>{
      const body={defer_services:true};
      if(source.value==='path'){
        if(!path.value.trim())throw Error('请填写服务器路径');
        body[kind.value==='bundle'?'dir':'image_path']=path.value.trim();
      }else{
        if(!file.files[0])throw Error('请选择文件');
        P.localMessage(message,'正在上传，请保持页面打开。文件接收后，可在后台继续处理。');
        if(kind.value==='bundle'){
          const uploaded=await api('/targets/import/bundle/upload?defer_extract=true&name='+encodeURIComponent(autoName()),'POST',file.files[0],true);
          if(uploaded.upload_id)body.bundle_upload_id=uploaded.upload_id;else body.dir=uploaded.dir;
        }else{
          const uploaded=await api('/targets/import/upload?rel_path='+encodeURIComponent(autoName()+'/'+file.files[0].name),'POST',file.files[0],true);
          body.image_path=uploaded.path;
        }
      }
      const job=await api('/jobs/import','POST',body);
      P.localMessage(message,'已提交后台任务。可以关闭弹窗，在导入任务列表查看进度。');
      await loadJobs();
      const poll=async()=>{
        if(!modal.dialog.open)return;
        try{
          const status=await api('/jobs/'+job.job_id);
          if(!modal.dialog.open)return;
          P.localMessage(message,status.message||importStatus[status.status]||status.status);
          if(['queued','running'].includes(status.status))setTimeout(poll,1500);
          else{
            await load();
            const refs=Object.keys(status.result?.pieces?.image?.receipt?.images||{});
            if(refs.length===1 && modal.dialog.open){modal.close();await openImage(refs[0]);}
          }
        }catch(error){P.localMessage(message,error.message,true);}
      };
      setTimeout(poll,1000);
    }),true));}
  load().catch(error=>P.notice(error.message,true));
})();
