const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const script = fs.readFileSync(path.join(__dirname, '../../static/topology-addressing.js'), 'utf8');
const {assign, warnings} = vm.runInNewContext(script + '\nTopologyAddressing');

const nodes = [
  {id:1, name:'主机甲'}, {id:2, name:'路由器'}, {id:3, name:'主机乙'},
  {id:4, name:'交换机', type:'switch'}, {id:5, name:'集线器', type:'hub'},
  {id:6, name:'主机丙'},
];
const links = [];
function connect(a, b) {
  const link = {node1_id:a, node2_id:b};
  assert.equal(assign(link, links, nodes), '');
  links.push(link);
  return link;
}
const subnet = iface => iface.ip4.split('.').slice(0, 3).join('.');

const direct = connect(1, 2);
const second = connect(2, 3);
assert.equal(subnet(second.iface1), subnet(second.iface2));
assert.notEqual(subnet(second.iface1), subnet(direct.iface1));
assert.equal(second.iface1.id, 1);

const shared = {
  node1_id:2, node2_id:4, iface1:{id:2, ip4:'192.168.10.10', ip4_mask:25},
};
links.push(shared);
connect(4, 5);
const throughHub = connect(5, 6);
assert.equal(subnet(throughHub.iface2), '192.168.10');
assert.equal(throughHub.iface2.ip4_mask, 25);
assert.equal(shared.iface1.ip4, '192.168.10.10');
assert.equal(warnings(links, nodes).length, 0);

second.iface2.ip4 = direct.iface1.ip4;
const found = warnings(links, nodes);
assert.ok(found.some(message => message.includes('网段不一致')));
assert.ok(found.some(message => message.includes('重复')));
console.log('Topology IPv4 allocation and warnings passed');
