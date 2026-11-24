const test = require('node:test');
const assert = require('node:assert/strict');
const h5 = require('../frontend/h5-workbench.js');

test('theme gallery has all 50 reference images and choosing a theme does not generate', () => {
  const fs=require('node:fs'),path=require('node:path');
  const files=fs.readdirSync(path.join(__dirname,'../frontend/h5-theme-previews')).filter(x=>x.endsWith('.jpg'));
  assert.equal(files.length,50);
  const selected={id:'cobalt',name:'Cobalt',preview:'/h5-theme-previews/cobalt.jpg'};
  assert.equal(h5.component.computed.selectedTheme.call({themes:[selected],theme:'cobalt'}),selected);
  assert.ok(h5.component.template.includes('theme=item.id;themeChooser=false'));
  assert.ok(h5.component.template.includes('主题设计参考，实际内容与排版随材料调整。'));
});

test('optional H5 navigation preserves the existing views and blocks the disabled entry', () => {
  const calls=[];
  const options={computed:{pageTitle(){return 'original '+this.activeView;}},methods:{switchView(view,...rest){calls.push([view,...rest]);return 'original';}}};
  h5.install(options);
  const ctx={activeView:'company-knowledge',h5Enabled:false,$refs:{},mobileNavOpen:true};
  assert.equal(options.methods.switchView.call(ctx,'research',{reloadProjects:false}),'original');
  assert.deepEqual(calls,[['research',{reloadProjects:false}]]);
  options.methods.switchView.call(ctx,'knowledge-h5');assert.equal(ctx.activeView,'company-knowledge');
  assert.equal(options.computed.pageTitle.call(ctx),'original company-knowledge');
  ctx.h5Enabled=true;options.methods.switchView.call(ctx,'knowledge-h5');
  assert.equal(ctx.activeView,'knowledge-h5');assert.equal(ctx.mobileNavOpen,false);
  assert.equal(options.computed.pageTitle.call(ctx),'H5 制作');
});

test('source shortcut is read-only permission based and does not accept its own H5 output', () => {
  assert.equal(h5.supported({can_read:true,can_manage:false,has_original:true,format:'DOCX'}),true);
  for(const change of [{can_read:false},{has_original:false},{format:'H5'},{format:'MP4'}])
    assert.equal(h5.supported({can_read:true,has_original:true,format:'DOCX',...change}),false);
});

test('preview keeps a unique origin and neither title confirmation nor advanced configuration', () => {
  const template=h5.component.template;
  assert.ok(template.includes('sandbox="allow-scripts"'));
  assert.ok(!template.includes('allow-same-origin'));
  assert.ok(!template.includes('v-html'));
  assert.ok(!template.includes('v-model="title"'));
  assert.ok(!template.includes('model="apiKey"'));
});

test('completed titles open the independent reader; other records show progress', async () => {
  const calls=[];const ctx={openReader:job=>calls.push(['reader',job.id]),action:fn=>fn(),select:job=>calls.push(['progress',job.id])};
  await h5.component.methods.openRecord.call(ctx,{id:'done',state:'ready'});
  await h5.component.methods.openRecord.call(ctx,{id:'stopped',state:'failed'});
  assert.deepEqual(calls,[['reader','done'],['progress','stopped']]);
  const fs=require('node:fs');const html=fs.readFileSync(require('node:path').join(__dirname,'../frontend/index.html'),'utf8');
  const menu=html.indexOf('index="knowledge-h5"');
  assert.ok(menu>html.indexOf('</el-sub-menu>',html.indexOf('<el-sub-menu index="company-knowledge"')));
  assert.ok(h5.component.template.includes('生成已暂停') || h5.component.methods.stateLabel({state:'failed'})==='生成已暂停，进度保留');
});

test('complete pending candidates open the reader and selected repairs send only their chapter', async () => {
  const calls=[];
  const ctx={current:{id:'candidate'},openReader:job=>calls.push(job.id),action:fn=>fn(),
    api:async(path,options)=>{calls.push([path,JSON.parse(options.body)]);return {id:'candidate'};},select:async()=>{}};
  await h5.component.methods.openRecord.call(ctx,{id:'candidate',state:'review_pending',candidate_available:true});
  await h5.component.methods.repairChapter.call(ctx,'3');
  assert.deepEqual(calls,['candidate',['/jobs/candidate/repair-chapter',{chapter:3}]]);
  assert.ok(h5.component.template.includes('current.state===\'ready\' && !current.saved_document_id'));
  assert.ok(h5.component.template.includes('完整草稿可阅读和下载'));
});
