const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture() {
  const nodes = new Map(), handlers = {}, notices = [];
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value:'',textContent:'',innerHTML:'',hidden:false,disabled:false,checked:true,
      handlers:{}, addEventListener(name,fn){this.handlers[name]=fn;}});
    return nodes.get(id);
  };
  const settings = {interval_seconds:60,lookback_minutes:5,confirmations:3,rank_jump:5,alerts_enabled:true,_revision:'one'};
  let data = {enabled:false,busy:false,settings,snapshot:null,events:[],history:[]};
  const sandbox = {console,window:{},document:{hidden:false,getElementById:node,addEventListener:(name,fn)=>handlers[name]=fn},
    setInterval(){},toast:(...args)=>notices.push(args),fetch:async()=>({ok:true,json:async()=>structuredClone(data)})};
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/market.js'),'utf8'),sandbox);
  handlers.DOMContentLoaded();
  return {node,sandbox,notices,load:()=>sandbox.window.marketMonitor.activate(),setData:d=>data=d,getData:()=>data};
}

test('empty market view shows missing values instead of zero',async()=>{
  const f=fixture();await f.load();
  assert.equal(f.node('marketScore').textContent,'—');
  assert.ok(f.node('marketMetrics').innerHTML.includes('— / — / —'));
  assert.equal(f.node('marketToggle').textContent,'启动市场监控');
});

test('shared settings polling preserves an unsaved draft',async()=>{
  const f=fixture();await f.load();
  f.node('marketInterval').value='30'; f.node('marketSettingsForm').handlers.input();
  f.setData({...f.getData(),settings:{...f.getData().settings,_revision:'two',interval_seconds:120}});
  await f.load();
  assert.equal(f.node('marketInterval').value,'30');
  assert.ok(f.node('marketMonitorStatus').textContent.includes('其他设备更新'));
});

test('network errors are visible and preserve the existing view',async()=>{
  const f=fixture(); await f.load();
  f.sandbox.fetch=async()=>{throw new Error('offline');};await f.load();
  assert.ok(f.node('marketMonitorStatus').textContent.includes('可能已过期'));
});

test('hostile event text is escaped and history is not toasted at first load',async()=>{
  const f=fixture();f.setData({...f.getData(),events:[{id:'1',at:'2026-08-31T10:00:00+08:00',message:'<img src=x onerror=alert(1)>'}]});
  await f.load();assert.ok(!f.node('marketEvents').innerHTML.includes('<img'));
  assert.ok(f.node('marketEvents').innerHTML.includes('&lt;img'));assert.equal(f.notices.length,0);
  f.setData({...f.getData(),events:[{id:'2',at:'2026-08-31T10:01:00+08:00',message:'已确认变化'}]});
  await f.load();assert.equal(f.notices.length,1);await f.load();assert.equal(f.notices.length,1);
});

test('sector ladder renders separated first-board positions and their evidence',async()=>{
  const f=fixture(), quality={source:'eastmoney',complete:true,received:3600,expected:3600,valid:3600,excluded:0,fresh_pct:100};
  const candidate={code:'600001',name:'补涨股',position_label:'低位补涨',pre_return_5d:1.2,pre_excess_5d:-3.4,
    distance_20d_high_pct:-1,prior_limit_count_10d:0,catch_up_confidence:'高',catch_up_score:81,
    position_reason:'<低位证据>'};
  f.setData({...f.getData(),snapshot:{phase:'continuous',captured_at:'2026-08-31T10:00:00+08:00',sentiment:null,
    sectors:[],sector_ladders:[{rank:1,name:'测试行业',change_pct:3,limit_up_count:1,broken_count:0,promotion_count:0,
      max_streak:1,up_ratio:70,amount_share:5,position_history_covered:1,position_history_verified:1,main_directions:[],
      ladder:[],promoted_stocks:[],broken_focus:[],low_catch_up_candidates:[candidate],high_rebound_candidates:[],
      trend_acceleration_candidates:[],follow_candidates:[],analysis:'已分类',data_complete:true}],
    pools:{up:{count:1},down:{count:0},broken:{count:0}},market_quality:quality,sector_quality:quality}});
  await f.load();const html=f.node('marketSectorLadders').innerHTML;
  assert.ok(html.includes('低位补涨'));assert.ok(html.includes('高位反包'));assert.ok(html.includes('趋势加速'));
  assert.ok(html.includes('封板前5日超额'));assert.ok(html.includes('补涨条件分 高 81'));
  assert.ok(html.includes('&lt;低位证据&gt;'));assert.ok(!html.includes('<低位证据>'));
});

test('market and sector leader scores are rendered separately with escaped evidence',async()=>{
  const f=fixture(), quality={source:'eastmoney',complete:true,received:3600,expected:3600,valid:3600,excluded:0,fresh_pct:100};
  const sectorLeader={code:'600001',name:'板块<龙>',streak:2,first_limit_time:'09:31:00',followers_after_limit:4,
    sector_leader_score:82,sector_leader_role:'板块龙已确认',sector_leader_components:{'板块带动代理':20,'事件身位':17},
    influence_observations:3,seal_float_ratio_pct:1.25};
  const marketLeader={...sectorLeader,name:'市场&龙',market_leader_score:78,market_leader_role:'市场投机龙候选',
    market_leader_components:{'市场空间':20,'全市场带动代理':16},attention_best_rank:2,attention_sources:['同花顺','雪球'],dual_leader:true};
  f.setData({...f.getData(),snapshot:{phase:'continuous',captured_at:'2026-08-31T10:00:00+08:00',sentiment:null,sectors:[],
    market_speculation_leaders:[marketLeader],leader_analysis:{method:'连续事件响应代理',hotlist_stock_count:1},
    sector_ladders:[{rank:1,name:'测试行业',change_pct:3,limit_up_count:1,broken_count:0,promotion_count:1,max_streak:2,
      up_ratio:70,amount_share:5,position_history_covered:0,position_history_verified:0,main_directions:[],ladder:[],
      promoted_stocks:[],broken_focus:[],low_catch_up_candidates:[],high_rebound_candidates:[],trend_acceleration_candidates:[],
      follow_candidates:[],sector_leader:sectorLeader,sector_leader_candidates:[sectorLeader],analysis:'已评分',data_complete:true}],
    pools:{up:{count:1},down:{count:0},broken:{count:0}},market_quality:quality,sector_quality:quality}});
  await f.load();
  const marketHtml=f.node('marketLeaderSummary').innerHTML, sectorHtml=f.node('marketSectorLadders').innerHTML;
  assert.ok(marketHtml.includes('市场投机龙候选'));assert.ok(marketHtml.includes('全市场带动代理'));assert.ok(marketHtml.includes('同花顺、雪球'));
  assert.ok(marketHtml.includes('市场&amp;龙'));assert.ok(!marketHtml.includes('市场&龙'));
  assert.ok(sectorHtml.includes('板块龙已确认'));assert.ok(sectorHtml.includes('板块带动代理'));assert.ok(sectorHtml.includes('查看板块龙候选评分证据'));
  assert.ok(sectorHtml.includes('板块&lt;龙&gt;'));assert.ok(!sectorHtml.includes('板块<龙>'));
});

test('a poll started before saving cannot revert newly saved settings',async()=>{
  const f=fixture();await f.load();
  let release;
  const old = structuredClone(f.getData());
  f.sandbox.fetch=async(_url,options)=>options?.method==='PUT' ? {ok:true,json:async()=>({...old.settings,_revision:'two',interval_seconds:30})}
    : new Promise(resolve=>release=()=>resolve({ok:true,json:async()=>old}));
  const pending=f.load();f.node('marketInterval').value='30';f.node('marketSettingsForm').handlers.input();
  await f.node('marketSettingsForm').handlers.submit({preventDefault(){}});
  release();await pending;
  assert.equal(f.node('marketInterval').value,'30');
});

test('editing during save preserves the newer draft',async()=>{
  const f=fixture();await f.load();let release;
  f.sandbox.fetch=async(_url,options)=>options?.method==='PUT' ? new Promise(resolve=>release=()=>resolve({ok:true,json:async()=>({...f.getData().settings,_revision:'two',interval_seconds:30})}))
    : {ok:true,json:async()=>f.getData()};
  f.node('marketInterval').value='30';f.node('marketSettingsForm').handlers.input();
  const saving=f.node('marketSettingsForm').handlers.submit({preventDefault(){}});
  f.node('marketInterval').value='120';f.node('marketSettingsForm').handlers.input();
  release();await saving;assert.equal(f.node('marketInterval').value,'120');
});
