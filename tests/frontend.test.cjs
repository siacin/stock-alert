const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture() {
  const nodes = new Map();
  const node = (selector) => {
    if (!nodes.has(selector)) nodes.set(selector, {textContent:'',hidden:false,className:'',classList:{toggle(){}},contains:(target)=>target === 'editing'});
    return nodes.get(selector);
  };
  const sandbox = {
    console, setTimeout, clearTimeout,
    document: {hidden:false,activeElement:null,addEventListener(){},querySelector:s=>s==='dialog[open]' ? null : node(s)},
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/app.js'),'utf8'), sandbox);
  const state = vm.runInContext('state',sandbox);
  vm.runInContext('renderStocks = () => {};', sandbox);
  return {sandbox,state,node,run:code=>vm.runInContext(code,sandbox)};
}

test('clean page accepts shared config and saved agent result',async()=>{
  const f=fixture(); f.state.config={_revision:'old'};
  f.sandbox.fetchShared=async path=>path==='/api/config' ? {_revision:'new',stocks:[]} : {result:{result_id:'result-1'}};
  f.run('api = fetchShared'); await f.run('syncSharedData()');
  assert.equal(f.state.config._revision,'new');
  assert.equal(f.state.newsAgentResult.result_id,'result-1');
  assert.equal(f.state.conflict,false);
});

test('dirty page preserves draft and reports conflict',async()=>{
  const f=fixture(); f.state.config={_revision:'old',stocks:[{cost:8}]}; f.state.dirty=true;
  f.sandbox.fetchShared=async path=>path==='/api/config' ? {_revision:'new',stocks:[{cost:9}]} : {result:null};
  f.run('api = fetchShared'); await f.run('syncSharedData()');
  assert.equal(f.state.config.stocks[0].cost,8);
  assert.equal(f.state.conflict,true);
  assert.equal(f.node('#syncConflict').hidden,false);
});

test('response from an old poll cannot roll back a newly saved revision',async()=>{
  const f=fixture(); f.state.config={_revision:'old'};
  let release;
  f.sandbox.fetchShared=path=>path==='/api/config' ? new Promise(resolve=>release=resolve) : Promise.resolve({result:null});
  f.run('api = fetchShared'); const pending=f.run('syncSharedData()');
  f.state.config={_revision:'just-saved'};
  release({_revision:'old'}); await pending;
  assert.equal(f.state.config._revision,'just-saved');
});

test('hidden page does not poll and failed requests preserve drafts',async()=>{
  const f=fixture(); let calls=0;
  f.sandbox.fetchShared=async()=>{calls++; throw new Error('offline');}; f.run('api = fetchShared');
  f.state.config={_revision:'draft'}; f.sandbox.document.hidden=true;
  await f.run('syncSharedData()'); assert.equal(calls,0);
  f.sandbox.document.hidden=false; await f.run('syncSharedData()');
  assert.equal(f.state.config._revision,'draft'); assert.equal(f.state.syncBusy,false);
});

test('status polling does not replace a focused stock input',()=>{
  const f=fixture(); let renders=0;
  f.sandbox.countRender=()=>renders++; f.run('renderStocks = countRender');
  f.state.status={state:'stopped',snapshot:{}};
  f.sandbox.document.activeElement='editing'; f.run('renderStatus()'); assert.equal(renders,0);
  f.sandbox.document.activeElement=null; f.run('renderStatus()'); assert.equal(renders,1);
});

function agentForm(f) {
  const fields = {newsAgentApiUrl:'https://llm.example',newsAgentModel:'deepseek-v4-flash',
    newsAgentTimeout:'180',newsAgentMaxNews:'60',newsAgentTemperature:'',newsAgentStreamMode:'auto',
    newsAgentThinkingMode:'auto',newsAgentOutputLimit:'8192',newsAgentApiKey:''};
  for (const [id,value] of Object.entries(fields)) f.node('#'+id).value=value;
  f.node('#newsAgentClearKey').checked=false;
  f.node('#newsAgentSettingsForm').reportValidity=()=>true;
  f.node('#testNewsAgentButton').disabled=false;
}

test('agent draft includes compatibility modes and omits optional temperature',()=>{
  const f=fixture();agentForm(f);
  const data=f.run('newsAgentSettingsPayload()');
  assert.equal(data.temperature,null);assert.equal(data.api_key,'');
  assert.equal(data.stream_mode,'auto');assert.equal(data.thinking_mode,'auto');
  assert.equal(data.request_timeout_seconds,180);assert.equal(data.max_output_tokens,8192);
  f.node('#newsAgentTemperature').value='0.3';
  assert.equal(f.run('newsAgentSettingsPayload()').temperature,0.3);
});

test('connection test sends only draft settings and renders provider text safely',async()=>{
  const f=fixture();agentForm(f);const calls=[];
  f.sandbox.probe=async(url,options)=>{
    calls.push([url,JSON.parse(options.body)]);
    return {model:'deepseek-v4-flash',latency_ms:63200,response_format:'sse',
      reply:'<img src=x onerror=alert(1)>',api_url:'https://llm.example/v1/chat/completions'};
  };
  f.run('api=probe');await f.run('testNewsAgentConnection()');
  assert.equal(calls.length,1);assert.equal(calls[0][0],'/api/news-agent/test');
  assert.equal(calls[0][1].api_url,'https://llm.example');
  assert.equal('watchlist' in calls[0][1],false);assert.equal('news' in calls[0][1],false);
  assert.match(f.node('#newsAgentTestResult').textContent,/63\.2 秒 · SSE/);
  assert.match(f.node('#newsAgentTestResult').textContent,/测试未保存/);
  assert.match(f.node('#newsAgentTestResult').textContent,/<img/);
  assert.equal(f.node('#newsAgentTestResult').innerHTML,undefined);
  assert.equal(f.node('#testNewsAgentButton').disabled,false);
});

test('connection button prevents duplicate requests until completion',async()=>{
  const f=fixture();agentForm(f);let calls=0,release;
  f.sandbox.probe=()=>{calls++;return new Promise(resolve=>release=resolve);};
  f.run('api=probe');const pending=f.run('testNewsAgentConnection()');
  assert.equal(f.node('#testNewsAgentButton').disabled,true);
  await f.run('testNewsAgentConnection()');assert.equal(calls,1);
  release({model:'x',latency_ms:1,response_format:'json',reply:'OK',api_url:'https://llm.example/v1/chat/completions'});
  await pending;assert.equal(f.node('#testNewsAgentButton').disabled,false);
});

test('connection errors are visible and invalid forms do not call API',async()=>{
  const f=fixture();agentForm(f);let calls=0;
  f.sandbox.probe=async()=>{calls++;throw new Error('API HTTP 401: invalid credential');};
  f.run('api=probe');f.node('#newsAgentSettingsForm').reportValidity=()=>false;
  await f.run('testNewsAgentConnection()');assert.equal(calls,0);
  f.node('#newsAgentSettingsForm').reportValidity=()=>true;
  await f.run('testNewsAgentConnection()');assert.equal(calls,1);
  assert.match(f.node('#newsAgentTestResult').textContent,/连接失败.*401/);
  assert.equal(f.node('#testNewsAgentButton').disabled,false);
});

test('opening saved agent settings clears key field and restores compatibility controls',async()=>{
  const f=fixture();agentForm(f);let shown=false;
  f.node('#newsAgentApiKey').value='unsaved-test-value';
  f.node('#newsAgentSettingsDialog').showModal=()=>shown=true;
  f.sandbox.readSettings=async()=>({settings:{api_url:'https://llm.example/v1/chat/completions',model:'x',
    configured:true,api_key_configured:true,temperature:null,stream_mode:'off',thinking_mode:'enabled',
    max_output_tokens:4096,request_timeout_seconds:240,max_news_items:40}});
  f.run('api=readSettings');await f.run('openNewsAgentSettings()');
  assert.equal(shown,true);assert.equal(f.node('#newsAgentApiKey').value,'');
  assert.equal(f.node('#newsAgentTemperature').value,'');
  assert.equal(f.node('#newsAgentStreamMode').value,'off');
  assert.equal(f.node('#newsAgentThinkingMode').value,'enabled');
  assert.equal(f.node('#newsAgentOutputLimit').value,4096);
});

test('detailed agent result renders classifications and escapes model text',()=>{
  const f=fixture();
  f.run(`renderNewsAgentResult(${JSON.stringify({structured:true,metadata:{model:'test',news_count:1,manual_news_provided:true},analysis:{
    overview:'事件概览',topic_summaries:[{topic_id:'manual-1#1',title:'机器人政策',direction:'利好',priority:'高',selected_for_deep_dive:true,summary:'设备受益',sectors:['机器人']},{topic_id:'manual-1#2',title:'消费政策',direction:'中性',priority:'低',selected_for_deep_dive:false,summary:'待细则',sectors:['消费'],deferred_reason:'本轮预算'}],event_profile:{title:'机器人政策',direction:'利好',event_type:'政策',summary:'<img src=x onerror=alert(1)>',key_facts:['明确事实'],unknowns:['补贴金额']},
    logic_closure:[{stage:'变化',status:'已确认',conclusion:'政策发布',evidence:['输入原文'],missing_or_risks:[]},{stage:'影响',status:'部分确认',conclusion:'设备需求可能增加',evidence:['政策方向'],missing_or_risks:['执行细则']},{stage:'业绩',status:'待核验',conclusion:'等待订单',evidence:[],missing_or_risks:['收入占比']},{stage:'股价定价',status:'部分确认',conclusion:'板块升温',evidence:['实时快照'],missing_or_risks:['持续性']}],logic_closure_summary:{grade:'B',event_stage:'披露',pricing_stage:'启动',overall:'影响链较清楚，业绩待验证',weakest_link:'业绩',next_upgrade_condition:'订单公告'},
    thesis_balance:{bull_case:'订单可能增长',bear_case:'采购落地不及预期',strongest_counterargument:'政策没有明确预算',resolution_data:['采购金额']},market_pricing:{stage:'启动',data_status:'实时',captured_at:'2026-09-02 10:30',event_price_alignment:'板块与事件共振',sector_evidence:['板块强度第一'],leader_evidence:['龙头三连板'],crowding_evidence:['炸板率上升'],limitations:['免费行情延迟']},evidence_ledger:[{claim:'需求改善',evidence:'政策原文',source_type:'输入原文',status:'已确认',supports:'变化'}],
    direction_deep_dives:[{direction:'机器人设备',conclusion:'设备需求提升',mechanism:'投资传导',value_chain:[{layer:'零部件',impact:'需求增加',beneficiary_profile:'核心零部件公司'}],economics:{formula:'新增利润=订单×净利率',known_inputs:['订单'],missing_inputs:['净利率']},candidate_categories:['零部件'],counter_arguments:['落地不及预期'],historical_analogs:[{event:'历史政策',lesson:'分化',source_status:'待核验'}]}],
    transmission_path:[{step:1,from:'政策',to:'机器人产业',logic:'研发投入增加',strength:'强'}],
    sector_impacts:[{sector:'机器人',industry_level:'主题',role:'中游',direction:'利好',strength:'强',logic:'需求增加',benefit_conditions:['订单落地'],risk_conditions:['政策取消']}],
    stock_buckets:{core:[{name:'核心公司',code:'000001',confidence:'高',reason:'直接受益'}],observation:[{name:'观察公司',reason:'等待公告'}],sentiment:[],negative:[],excluded:[{name:'名称相似公司',reason:'无业务证据'}]},
    sector_ladders:[{sector:'机器人',tiers:[{tier:'核心',stocks:[{name:'核心公司',reason:'订单明确'}]}]}],
    validation_signals:{confirmed:['政策发布'],to_verify:['订单'],invalidation:['政策撤回']},signal_board:{verify:[{signal:'订单落地',data_source:'公告',upgrade_effect:'业绩段升级'}],falsify:[{signal:'采购取消',data_source:'官方通知',downgrade_effect:'主逻辑失效'}],next_catalysts:[{event:'招标',window:'一个月',what_to_watch:'采购金额'}]},decision_chain:[{step:'事件定性',conclusion:'政策利好',evidence_status:'已确认'},{step:'确认点',conclusion:'订单公告',evidence_status:'待核验'}],scenarios:[{name:'基准',probability:'中',conditions:['执行落地'],market_impact:'板块分化'}],coverage_audit:{directions_checked:['设备'],candidate_count:12,included_count:2,excluded_count:1,unresolved_categories:['材料'],deferred_topics:[{title:'消费政策',reason:'本轮预算'}]},topic_results:[{topic_id:'u-failed',title:'独立失败方向',ok:false,error:'该方向流式连接中断',analysis:{}}],risks:['不构成建议']
  }})})`);
  const html=f.node('#newsAgentResult').innerHTML;
  for(const label of ['集合主题总览','本轮简析','事件画像','逻辑闭环审计','B级闭环','最强正方逻辑','最可能推翻主逻辑','当前市场定价检查','关键证据账本','单方向深挖','弹性公式','逻辑传导链','关联板块','主攻 · 逻辑最强','观察 · 强关联待验证','排除项','板块梯队','事实核验','动态信号板','决策链','情景推演','覆盖审计','本轮未深挖','逐方向独立分析','该方向未影响其他结果']) assert.match(html,new RegExp(label));
  assert.ok(!html.includes('<img src=x'));assert.ok(html.includes('&lt;img'));
});

test('manual-only analysis does not load radar and sends explicit context flag',async()=>{
  const f=fixture();let request;
  f.state.newsAgentSettings={configured:true};
  f.node('#newsAgentUserNews').value='一条用户粘贴的新闻';
  f.node('#newsAgentIncludeRadar').checked=false;
  f.node('#newsAgentAnalysisMode').value='deep';
  f.node('#newsAgentQuestion').value='详细分类';
  f.node('#newsAgentRunButton').disabled=false;
  f.sandbox.noRadar=()=>{throw new Error('radar should not load');};
  f.sandbox.agentApi=async(url,options)=>{request=[url,JSON.parse(options.body)];return {structured:true,analysis:{overview:'完成'},metadata:{}};};
  f.run('loadRadar=noRadar;api=agentApi');await f.run('runNewsAgent()');
  assert.equal(request[0],'/api/news-agent/analyze');
  assert.equal(request[1].user_news,'一条用户粘贴的新闻');assert.equal(request[1].include_radar,false);
  assert.equal(request[1].analysis_mode,'discover');
  assert.equal('deep_topic_limit' in request[1],false);
  assert.deepEqual(request[1].item_ids,[]);assert.equal(f.node('#newsAgentRunButton').textContent,'重新分析产业方向');
});

test('direction discovery renders selection board and selected units start independent deep calls',async()=>{
  const f=fixture();let request;
  const unit={unit_id:'u1',title:'硅光子产业链',category:'产业',direction:'利好',priority:'高',source_topic_ids:['manual-1#1'],source_facts:['硅光子成为主流'],reason:'架构切换带动器件需求'};
  const discovery={structured:true,metadata:{analysis_mode:'discover',model:'test',planned_unit_count:1},analysis:{overview:'发现一个方向',analysis_units:[unit],risks:['个股待分析']}};
  f.state.newsAgentDirectionPlan={units:[unit],request:{question:'深挖',user_news:'1.硅光子成为主流',include_radar:false,analysis_mode:'deep',item_ids:[]}};
  f.run(`renderNewsAgentResult(${JSON.stringify(discovery)})`);
  const discoveryHtml=f.node('#newsAgentResult').innerHTML;
  for(const label of ['发现 1 个产业方向','选择深挖方向','没有自动主题上限','硅光子产业链','产业传导逻辑','全选','分析选中方向']) assert.match(discoveryHtml,new RegExp(label));
  f.sandbox.agentApi=async(url,options)=>{request=[url,JSON.parse(options.body)];return {structured:true,metadata:{analysis_mode:'deep',selected_unit_count:1},analysis:{overview:'完成',topic_results:[]}};};
  f.run('api=agentApi');await f.run(`runSelectedNewsAgent(${JSON.stringify([unit])})`);
  assert.equal(request[0],'/api/news-agent/analyze');
  assert.equal(request[1].analysis_mode,'deep');
  assert.equal(request[1].selected_units[0].unit_id,'u1');
  assert.equal('deep_topic_limit' in request[1],false);
});
