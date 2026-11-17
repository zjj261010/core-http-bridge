/* Shared presentation behavior. Business actions remain in the existing pages. */
(() => {
  'use strict';
  const embedded = window.self !== window.top;
  document.documentElement.classList.toggle('embedded', embedded);
  const nav = document.querySelector('.bridge-page > .nav');
  nav?.querySelectorAll('a').forEach(link=>{if(link.getAttribute('href')==='/static/publications.html'||link.getAttribute('href')==='/#topo')link.remove();else if(link.getAttribute('href')==='/static/topologies.html')link.textContent='拓扑管理';});
  if (nav && !embedded) document.body.prepend(nav);
  const toggle = document.getElementById('workspaceNavToggle');
  function closeNav() {
    document.body.classList.remove('nav-open');
    toggle?.setAttribute('aria-expanded', 'false');
  }
  toggle?.addEventListener('click', () => {
    const open = document.body.classList.toggle('nav-open');
    toggle.setAttribute('aria-expanded', String(open));
    if (open) document.querySelector('.side-tab.on')?.focus();
  });
  document.getElementById('workspaceNavBackdrop')?.addEventListener('click', closeNav);
  document.querySelectorAll('.side-tab').forEach(tab => {
    tab.addEventListener('click', closeNav);
    tab.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); tab.click(); }
    });
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && document.body.classList.contains('nav-open')) { closeNav(); toggle?.focus(); }
  });
})();
