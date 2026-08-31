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
  vm.runInContext('renderStocks = () => {}; renderNewsAgentResult = payload => {state.newsAgentResult=payload;};', sandbox);
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
