// DOM/transport simulation only; no browser rendering or external requests.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('sandbox-review-ui.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const scenario = process.argv[2];
const elements = new Map();
for (const [, id] of html.matchAll(/\bid="([^"]+)"/g)) {
  assert(!elements.has(id), 'element IDs must be unique');
  elements.set(id, {disabled:false, hidden:false, textContent:'', value:'synthetic question',
    classes:new Set(), handlers:{}, focus() {this.focused=true;},
    addEventListener(event, fn) {this.handlers[event]=fn;}});
  const element=elements.get(id);
  element.classList={toggle(name,on) {if(on) element.classes.add(name);else element.classes.delete(name);}};
}
const el = id => {assert(elements.has(id), 'Missing real DOM element: '+id); return elements.get(id);};
let state = {question_version:0,new_question_enabled:true,client_kind:'stub',
  generation_attempted:false,live_refresh_enabled:false,live_refresh_status:'disabled',
  review_status:'not_generated',source:'synthetic_order_fixture',order_id:'EXAMPLEORDER',
  order_status:'CREATED',payment_truth:'CREATED_NOT_APPROVED',capture_status:null,
  observed_at:'2026-01-01T00:00:00Z',amount:'10.00',currency:'USD',draft:null};
const responseText='Earlier order observation. No captured payment recorded.';
const makeDraft=()=>({text:responseText,digest:'fixture-digest',question_intent:'payment_status',
  seller_next_step:'Check the payment record before confirming payment.',seller_attention:[]});
const staleScenario=scenario==='stale-live-stub'||scenario==='stale-live-granite'||scenario==='stale-before-generation';
if (staleScenario) {
  state={...state,client_kind:scenario==='stale-live-granite'?'granite':'stub',
    new_question_enabled:false,live_refresh_enabled:true,live_refresh_status:'stale',
    live_facts_fresh:false,source:'reviewer_live_paypal_sandbox_read',
    generation_attempted:scenario!=='stale-before-generation',
    review_status:'pending_human_review',draft:scenario==='stale-before-generation'?null:makeDraft()};
}
if (scenario==='completed-ready'||scenario==='completed-stale') {
  state={...state,completed_flow:true,completed_buyer_question:'Has this $10 order been paid?',
    model_execution_verified:true,client_kind:'granite',model_name:'granite4.2:3b',
    new_question_enabled:false,generation_attempted:true,live_refresh_enabled:false,
    live_facts_fresh:scenario==='completed-ready',source:'reviewer_live_paypal_sandbox_read',
    review_status:'pending_human_review',draft:makeDraft()};
}
if (scenario==='saved-record') state={...state,new_question_enabled:false,source:'saved_paypal_sandbox_receipt'};
let failState=scenario==='initial-unavailable';
const requests=[];
let held;
const response=value=>({ok:true,json:async()=>value});
const rejection=error=>({ok:false,json:async()=>({error})});
async function fetch(path,options) {
  requests.push({path,method:options?.method||'GET',data:options?.body?JSON.parse(options.body):null});
  if (path==='/api/state') {
    if (scenario==='initial-pending'&&requests.length===1) return new Promise(resolve=>{held=resolve;});
    if (failState) throw new Error('local connection closed');
    return response({...state});
  }
  if (path==='/api/generate') {
    if (scenario==='validation-rejected') return rejection('Enter one question of at most 500 characters.');
    if (scenario==='normalized-question-rejected') return rejection('Enter a question of 1–500 characters.');
    if (scenario==='unknown-api-error') return rejection('private service error must not appear');
    if (['stale-question-rejected','stale-question-with-result','stale-question-with-failure','stale-question-pending'].includes(scenario)) {
      state={...state,question_version:1,generation_attempted:scenario!=='stale-question-rejected',
        draft:scenario==='stale-question-with-result'?makeDraft():null};
      if (scenario==='stale-question-with-failure') state.generation_failure={stage:'admission',
        code:'RESOURCE_ADMISSION_DENIED',request_attempted:false,attempt_count:0};
      return rejection('This question changed in another tab. Reload session status before continuing.');
    }
    if (['generation-failed','stub-generation-failed','request-failed'].includes(scenario)) {
      state={...state,client_kind:scenario==='stub-generation-failed'?'stub':'granite',
        new_question_enabled:scenario==='stub-generation-failed',generation_attempted:true,
        generation_failure:scenario==='request-failed'
          ?{stage:'model_request',code:'MODEL_REQUEST_FAILED',request_attempted:true,attempt_count:1}
          :scenario==='stub-generation-failed'
            ?{stage:'model_request',code:'MODEL_SETUP_ERROR',request_attempted:null,attempt_count:null}
            :{stage:'admission',code:'RESOURCE_ADMISSION_DENIED',request_attempted:false,attempt_count:0}};
      throw new Error('private service error must not appear');
    }
    state={...state,generation_attempted:true,review_status:'pending_human_review',
      draft:scenario==='generation-empty-result'?null:makeDraft()};
    if (scenario==='generate-pending') return new Promise(resolve=>{held=resolve;});
    if (scenario==='generate-success'||scenario==='generation-empty-result') return response({...state});
    if (scenario==='generate-lost-state-unavailable') failState=true;
    throw new Error('Response interrupted.');
  }
  if (path==='/api/new-question') {
    state={...state,question_version:state.question_version+1,generation_attempted:false,
      draft:null,review_status:'not_generated'};
    if (scenario==='new-question-lost') throw new Error('Response interrupted.');
    return response({...state});
  }
  if (path==='/api/refresh') {
    if (scenario==='refresh-success') {
      state={...state,live_refresh_status:'succeeded',live_facts_fresh:true,
        source:'reviewer_live_paypal_sandbox_read',new_question_enabled:false};
      return response({...state});
    }
    state={...state,live_refresh_status:'failed',live_refresh_failure:{stage:'order_get',
      code:'ORDER_HTTP_REJECTED',http_status:404,
      call_counts:{oauth_attempts:1,pinned_order_get_attempts:1}}};
    throw new Error('Refresh stopped.');
  }
  throw new Error('Unexpected route: '+path);
}
const context={document:{getElementById:el},fetch};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function click(id) {assert.equal(el(id).disabled,false,id+' must be available');await el(id).handlers.click();await tick();}
(async()=>{
 vm.runInNewContext(script,context);
 if (scenario==='initial-pending') {
   for (const id of ['generate','buyer-question','refresh','new-question','reload-state']) assert.equal(el(id).disabled,true);
   held(response(state));await tick();assert.equal(el('generate').disabled,false);
 } else {
   await tick();
   if (scenario==='completed-ready'||scenario==='completed-stale') {
     assert.equal(el('buyer-question').value,'Has this $10 order been paid?');
     assert.equal(el('generate').hidden,true);assert.equal(el('generate').disabled,true);
     assert.equal(el('new-question').hidden,true);assert.equal(el('live-refresh-panel').hidden,true);
     assert.equal(el('draft').textContent,responseText);
     assert.equal(el('source').textContent,'PayPal Sandbox');
     assert.equal(el('status').textContent,scenario==='completed-stale'?'Order check expired':'Response ready.');
     await click('reload-state');
     assert.equal(el('buyer-question').value,'Has this $10 order been paid?');
     assert.equal(state.review_status,'pending_human_review');
   } else if (['generation-failed','stub-generation-failed','request-failed'].includes(scenario)) {
     await click('generate');
     assert.equal(el('session-details').hidden,false);assert.equal(el('generate').disabled,true);
     assert.equal(el('new-question').disabled,true);assert.equal(el('draft').textContent,'No response available.');
     assert.match(el('granite-diagnostic').textContent,/No retry was made/);
     assert.doesNotMatch(el('status').textContent,/ready|generated|timeout|timed out/i);
     if (scenario==='generation-failed') {
       assert.match(el('granite-diagnostic').textContent,/RESOURCE_ADMISSION_DENIED/);
       assert.match(el('granite-diagnostic').textContent,/request attempted: no; attempts: 0/);
     } else if (scenario==='stub-generation-failed') {
       assert.match(el('granite-diagnostic').textContent,/request attempted: unknown; attempts: unknown/);
     } else {
       assert.equal(el('status').textContent,'Response generation was interrupted.');
       assert.match(el('granite-diagnostic').textContent,/request attempted: yes; attempts: 1/);
     }
     await click('reload-state');
     assert.equal(requests.filter(r=>r.path==='/api/generate').length,1);
   } else if (['validation-rejected','normalized-question-rejected','unknown-api-error'].includes(scenario)) {
     const question='問'.repeat(401);
     assert.equal(question.length,401);assert.equal(Buffer.byteLength(question,'utf8'),1203);
     el('buyer-question').value=question;
     await click('generate');
     assert.equal(el('buyer-question').value,question);
     assert.equal(el('buyer-question').disabled,false);assert.equal(el('generate').disabled,false);
     assert.equal(state.generation_attempted,false);assert.equal(state.generation_failure,undefined);
     assert.equal(el('session-details').hidden,true);
     assert.equal(el('draft').textContent,'Your response will appear here.');
     if (scenario==='unknown-api-error') {
       assert.equal(el('status').textContent,'Status recovered. No retry was made.');
     } else {
       assert.equal(el('status').textContent,'Shorten your question and try again.');
       assert.equal(el('buyer-question').focused,true);
     }
     assert.equal(requests.filter(r=>r.path==='/api/generate').length,1);
     assert.equal(requests.filter(r=>r.path==='/api/state').length,2);
     await click('reload-state');
     assert.equal(requests.filter(r=>r.path==='/api/generate').length,1);
   } else if (['stale-question-rejected','stale-question-with-result','stale-question-with-failure','stale-question-pending'].includes(scenario)) {
     el('buyer-question').value='The old question';await click('generate');
     assert.equal(state.question_version,1);
     assert.equal(el('buyer-question').value,'');
     const attempted=scenario!=='stale-question-rejected';
     assert.equal(el('buyer-question').disabled,attempted);assert.equal(el('generate').disabled,attempted);
     if (scenario==='stale-question-rejected') {
       assert.equal(el('status').textContent,'The question changed in another tab. Enter your question again.');
       assert.equal(el('buyer-question').focused,true);
     } else if (scenario==='stale-question-with-result') {
       assert.equal(el('status').textContent,'The question changed in another tab. The current response is shown below.');
       assert.equal(el('draft').textContent,responseText);
       assert.match(el('buyer-question').placeholder,/earlier in this session/);
     } else if (scenario==='stale-question-pending') {
       assert.equal(el('status').textContent,'The question changed in another tab. Check status for its response.');
       assert.equal(el('draft').textContent,'No response available.');
     } else {
       assert.equal(el('status').textContent,'Response generation is unavailable on this computer.');
       assert.equal(el('generate').disabled,true);assert.equal(el('session-details').hidden,false);
     }
     assert.equal(requests.filter(r=>r.path==='/api/generate').length,1);
     assert.equal(requests.find(r=>r.path==='/api/generate').data.question_version,0);
     assert.equal(requests.filter(r=>r.path==='/api/state').length,2);
     await click('reload-state');
     assert.equal(requests.filter(r=>r.path==='/api/generate').length,1);
   } else if (scenario==='initial-ready') {
     assert.equal(el('status').textContent,'');assert.equal(el('environment').textContent,'Sample order');
     assert.equal(el('source').textContent,'Sample data');
     assert.equal(el('session-details').hidden,true);
     await click('generate');
     vm.runInNewContext(script,context);await tick();
     assert.equal(el('status').textContent,'Response ready.');
     assert.equal(el('buyer-question').value,'');
     assert.match(el('buyer-question').placeholder,/earlier in this session/);
     assert.equal(requests.filter(r=>r.path==='/api/generate').length,1);
   } else if (scenario==='new-question'||scenario==='new-question-lost') {
     await click('generate');assert.equal(el('new-question').disabled,false);
     await click('new-question');assert.equal(el('buyer-question').disabled,false);assert.equal(el('buyer-question').value,'');
     assert.equal(el('new-question').disabled,true);assert.equal(el('draft').textContent,'Your response will appear here.');
     assert.equal(requests.filter(r=>r.path==='/api/new-question').length,1);
     el('buyer-question').value='When will my package arrive?';await click('generate');
     const generations=requests.filter(r=>r.path==='/api/generate');
     assert.equal(generations.length,2);assert.deepEqual(generations.map(r=>r.data.question_version),[0,1]);
     assert.deepEqual(generations.map(r=>Object.keys(r.data).sort()),[['question','question_version'],['question','question_version']]);
   } else if (scenario==='initial-unavailable') {
     assert.equal(el('generate').disabled,true);assert.match(el('status').textContent,/unavailable/);
     failState=false;await click('reload-state');assert.equal(el('generate').disabled,false);
   } else if (scenario==='generate-lost'||scenario==='generate-lost-state-unavailable') {
     await click('generate');assert.equal(el('generate').disabled,true);
     if (failState) {
       assert.equal(el('new-question').disabled,true);assert.match(el('status').textContent,/unavailable/);
       failState=false;await click('reload-state');
     }
     assert.equal(el('buyer-question').disabled,true);assert.equal(el('draft').textContent,responseText);
     assert.equal(state.review_status,'pending_human_review');
     await click('reload-state');assert.equal(requests.filter(r=>r.path==='/api/generate').length,1);
   } else if (scenario==='refresh-lost'||scenario==='refresh-success') {
     state={...state,new_question_enabled:false,live_refresh_enabled:true,live_refresh_status:'not_requested'};
     await click('reload-state');assert.equal(el('generate').disabled,true);await click('refresh');
     assert.equal(el('refresh').disabled,true);
     assert.equal(el('generate').disabled,scenario==='refresh-lost');
     if (scenario==='refresh-lost') {
       assert.equal(el('status').textContent,'Couldn’t verify the order.');
       assert.match(el('refresh-diagnostic').textContent,/HTTP 404/);
       assert.match(el('refresh-diagnostic').textContent,/Authentication attempts: 1; order-read attempts: 1/);
       assert.equal(el('environment').textContent,'Sample order');
     } else assert.equal(el('source').textContent,'PayPal Sandbox');
     await click('reload-state');assert.equal(requests.filter(r=>r.path==='/api/refresh').length,1);
   } else if (staleScenario) {
     assert.equal(el('source').textContent,'PayPal Sandbox');
     assert.equal(el('status').textContent,'Order check expired');
     assert.equal(el('generate').disabled,true);assert.equal(el('refresh').disabled,true);
     assert.equal(el('new-question').hidden,true);await click('reload-state');
   } else if (scenario==='generate-success'||scenario==='generation-empty-result') {
     await click('generate');assert.equal(el('generate').disabled,true);
     if (scenario==='generate-success') {
       assert.equal(el('draft').textContent,responseText);assert.equal(el('status').textContent,'Response ready.');
       assert.equal(el('draft').focused,true);assert.equal(el('seller-guidance').hidden,false);
     } else {
       assert.equal(el('draft').textContent,'No response available.');
       assert.equal(el('status').textContent,'No response is available.');
       assert.equal(el('new-question').disabled,true);
     }
   } else if (scenario==='generate-pending') {
     const pending=click('generate');await tick();
     for (const id of ['generate','buyer-question','reload-state','new-question','refresh']) assert.equal(el(id).disabled,true);
     // Even a direct second handler call cannot bypass the in-flight lock.
     await el('generate').handlers.click();await el('reload-state').handlers.click();
     assert.equal(requests.filter(r=>r.path==='/api/generate').length,1);
     held(response({...state}));await pending;
     assert.equal(el('draft').textContent,responseText);
   } else if (scenario==='empty-question') {
     el('buyer-question').value='  ';await click('generate');
     assert.equal(el('status').textContent,'Enter a customer question.');
     assert.equal(el('buyer-question').focused,true);assert.equal(el('generate').disabled,false);
   } else if (scenario==='saved-record') {
     assert.equal(el('environment').textContent,'Sandbox order');
     assert.equal(el('source').textContent,'Saved Sandbox record');
     assert.equal(el('facts-source-note').textContent,'Historical order record');
     assert.equal(el('new-question').hidden,true);
   } else throw new Error('Unknown scenario');
 }
 assert.equal(elements.has('review'),false);
 assert.equal(elements.has('review-state'),false);
 assert.equal(elements.has('admission-scope'),false);
 assert(!requests.some(r=>r.path==='/api/review'));
 assert(requests.every(r=>['/api/state','/api/generate','/api/new-question','/api/refresh'].includes(r.path)));
 assert(!Array.from(elements.values()).some(e=>e.textContent.includes('private service error')));
 const posts=requests.filter(r=>r.method==='POST');
 const expectedPosts=['new-question','new-question-lost'].includes(scenario)?3
   :['initial-ready','generation-failed','stub-generation-failed','request-failed','generate-lost',
     'generate-lost-state-unavailable','refresh-lost','refresh-success','generate-success',
     'generation-empty-result','generate-pending','validation-rejected','normalized-question-rejected',
     'unknown-api-error','stale-question-rejected','stale-question-with-result','stale-question-with-failure','stale-question-pending'].includes(scenario)?1:0;
 assert.equal(posts.length,expectedPosts);
 for (const request of posts) {
   if(request.path==='/api/generate') assert.deepEqual(Object.keys(request.data).sort(),['question','question_version']);
   else if(request.path==='/api/new-question') assert.deepEqual(Object.keys(request.data),['question_version']);
   else assert.deepEqual(request.data,{});
 }
 console.log('PASS '+scenario+' — simulated DOM/fetch, zero external requests');
})().catch(error=>{console.error(error);process.exitCode=1;});
