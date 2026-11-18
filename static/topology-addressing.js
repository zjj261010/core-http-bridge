/* Allocate IPv4 per broadcast segment and keep manually configured interfaces intact. */
const TopologyAddressing = (() => {
  const ends = ['iface1', 'iface2'];
  const nodeId = (link, end) => link[end === 'iface1' ? 'node1_id' : 'node2_id'];
  const isNetwork = node => ['switch', 'hub', 'wlan', 'wireless_lan'].includes(node.type?.toLowerCase());
  const ipText = ip => [24, 16, 8, 0].map(shift => (ip >>> shift) & 255).join('.');
  const overlaps = (a, b) => a.first <= b.last && b.first <= a.last;

  function address(link, end) {
    const text = link[end]?.ip4 ?? link[end + '_ip4'];
    const prefix = Number(link[end]?.ip4_mask ?? link[end + '_ip4_mask'] ?? 24);
    if (typeof text !== 'string' || !/^(?:\d{1,3}\.){3}\d{1,3}$/.test(text) || !Number.isInteger(prefix) ||
        prefix < 0 || prefix > 32) return null;
    const octets = text.split('.').map(Number);
    if (octets.some(value => value > 255)) return null;
    const ip = octets.reduce((value, octet) => value * 256 + octet, 0);
    const size = 2 ** (32 - prefix);
    const first = Math.floor(ip / size) * size;
    return {ip, prefix, first, last:first + size - 1, node:nodeId(link, end)};
  }

  function segments(links, networks) {
    const neighbors = new Map();
    for (const link of links) {
      for (const id of [link.node1_id, link.node2_id]) {
        if (!networks.has(id)) continue;
        if (!neighbors.has(id)) neighbors.set(id, []);
        neighbors.get(id).push(link);
      }
    }
    const seen = new Set(), visitedNetworks = new Set(), groups = [];
    for (const link of links) {
      if (seen.has(link)) continue;
      const group = [link];
      seen.add(link);
      for (const current of group) {
        for (const id of [current.node1_id, current.node2_id]) {
          if (visitedNetworks.has(id)) continue;
          visitedNetworks.add(id);
          for (const neighbor of neighbors.get(id) || []) {
            if (seen.has(neighbor)) continue;
            seen.add(neighbor);
            group.push(neighbor);
          }
        }
      }
      groups.push(group);
    }
    return groups;
  }

  const addresses = links => links.flatMap(link => ends.map(end => address(link, end)).filter(Boolean));
  const networkIds = nodes => new Set(nodes.filter(isNetwork).map(node => node.id));

  function assign(link, links, nodes) {
    const networks = networkIds(nodes);
    const segment = segments([...links, link], networks).find(group => group.includes(link));
    const existing = addresses(segment);
    let subnet = existing[0];
    const usable = ends.filter(end => !networks.has(nodeId(link, end)));
    if (!usable.length) return '';
    if (!subnet) {
      const occupied = addresses(links);
      for (let first = 0x0a5a0000; first < 0x0b000000; first += 256) {
        const candidate = {first, last:first + 255, prefix:24};
        if (occupied.some(item => overlaps(item, candidate))) continue;
        subnet = candidate;
        break;
      }
    }
    const used = new Set(existing.map(item => item.ip));
    const assignments = [];
    for (const end of usable) {
      const id = nodeId(link, end);
      const ids = links.flatMap(other => ends.filter(side => nodeId(other, side) === id)
        .map(side => other[side]?.id)).map((value, index) => value ?? index);
      link[end] = {id:Math.max(-1, ...ids) + 1};
      if (!subnet || subnet.prefix === 32 ||
          existing.some(item => item.first !== subnet.first || item.prefix !== subnet.prefix)) continue;
      const lower = subnet.first + (subnet.prefix === 31 ? 0 : 1);
      const upper = subnet.last - (subnet.prefix === 31 ? 0 : 1);
      const preferred = subnet.first + ((id - 1) % 250 + 250) % 250 + 1;
      let ip = preferred >= lower && preferred <= upper && !used.has(preferred) ? preferred : lower;
      while (ip <= upper && used.has(ip)) ip++;
      if (ip > upper) continue;
      used.add(ip);
      assignments.push([end, ip]);
    }
    if (assignments.length !== usable.length) return '该链路所在网络的网段不一致或地址不足，请手动设置 IPv4。';
    for (const [end, ip] of assignments) Object.assign(link[end], {ip4:ipText(ip), ip4_mask:subnet.prefix});
    return '';
  }

  function warnings(links, nodes) {
    const names = new Map(nodes.map(node => [node.id, node.name]));
    const label = id => names.get(id) || '#' + id;
    const issues = [], seen = new Map(), segmentsByNode = new Map();
    for (const group of segments(links, networkIds(nodes))) {
      const configured = addresses(group);
      const first = configured[0];
      if (first && configured.some(item => item.first !== first.first || item.prefix !== first.prefix)) {
        issues.push('同一链路或交换网络内的 IPv4 网段不一致：' +
          [...new Set(configured.map(item => label(item.node)))].join('、'));
      }
      for (const item of configured) {
        const prior = seen.get(item.ip);
        if (prior) issues.push('IPv4 ' + ipText(item.ip) + ' 重复：' + label(prior.node) + '、' + label(item.node));
        seen.set(item.ip, item);
        const connected = segmentsByNode.get(item.node) || [];
        if (connected.some(entry => entry.group !== group && overlaps(entry.item, item))) {
          issues.push(label(item.node) + ' 的不同网络使用了重叠 IPv4 网段。');
        }
        connected.push({group, item});
        segmentsByNode.set(item.node, connected);
      }
    }
    return [...new Set(issues)];
  }

  return {assign, warnings};
})();
