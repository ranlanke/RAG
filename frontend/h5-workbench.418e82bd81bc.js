/* The company app owns authentication; H5 runs only in an opaque-origin frame. */
const H5Workbench = (() => {
  const base = '/company-knowledge/h5';
  const supported = doc => doc?.can_read && doc.has_original && /^(PDF|DOCX|TXT|MD|PNG|JPG|JPEG|WEBP)$/.test(doc.format);
  const requestKey = () => globalThis.crypto?.randomUUID?.() || Date.now().toString(36)+Math.random().toString(36).slice(2);
  const component = {
    props: { request: Function, entry: Object }, emits: ['location'],
    data() { return { source: null, file: null, theme: 'cobalt', themes: [], key: requestKey(), busy: false,
      error: '', ready: false, records: [], recordPage: 1, recordTotal: 0, current: null, html: '', previewUrl: '', width: 'desktop',
      themeChooser: false, chooser: false, query: '', sourcePage: 1, sourceTotal: 0, sources: [], saveOpen: false,
      projects: [], folders: [], projectId: '', folderId: '', alive: true, poll: null }; },
    computed: {
      selectedTheme() { return this.themes.find(item => item.id === this.theme); },
      currentFolders() { return this.folders.filter(f => f.project_id === this.projectId); },
      canGenerate() { return !!(this.source || this.file) && !this.busy; },
    },
    watch: { entry(value) { if (this.ready) this.applyEntry(value); }, projectId() { this.folderId = ''; },
      theme() { this.key = requestKey(); } },
    async mounted() {
      await Promise.allSettled([this.loadRecords(), this.loadThemes()]);
      this.ready = true;
      await this.applyEntry(this.entry);
    },
    beforeUnmount() { this.alive = false; clearTimeout(this.poll); this.html = ''; if(this.previewUrl)URL.revokeObjectURL(this.previewUrl); },
    methods: {
      async api(path, options = {}) {
        const response = await this.request(base+path, options);
        const data = await response.json();
        if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : '操作未完成，请检查材料或稍后重试。');
        return data;
      },
      async action(task) { this.busy = true; this.error = ''; try { await task(); } catch(e) { this.error = e.message; } finally { this.busy = false; } },
      async loadThemes() { try { this.themes = (await this.api('/themes')).themes; } catch(e) { this.error = e.message; } },
      async loadRecords() {
        const result = await this.api('/jobs?page='+this.recordPage);
        if (!this.alive) return;
        this.records = result.items; this.recordTotal = result.total;
      },
      async applyEntry(value) {
        if (!value) return;
        if (value.kind === 'generate' && supported(value.document)) {
          this.source = value.document; this.file = null; this.key = requestKey();
          await this.generate();
        } else if (value.kind === 'saved') {
          await this.action(async () => this.select(await this.api('/saved/'+encodeURIComponent(value.id))));
        }
      },
      async openChooser() { this.chooser = true; this.sourcePage = 1; await this.action(() => this.loadSources()); },
      async loadSources() {
        const result = await this.api('/sources?page='+this.sourcePage+'&q='+encodeURIComponent(this.query));
        this.sources = result.items; this.sourceTotal = result.total;
      },
      chooseSource(doc) { this.source = doc; this.file = null; this.key = requestKey(); this.chooser = false; },
      chooseFile(event) {
        const file = event.target.files?.[0]; if (!file) return;
        if (file.size > 40*1024*1024) { this.error = '文件不能超过 40 MB。'; event.target.value = ''; return; }
        this.file = file; this.source = null; this.key = requestKey(); this.error = '';
      },
      async generate() {
        if (!this.canGenerate) return;
        await this.action(async () => {
          let result;
          if (this.source) result = await this.api('/jobs', {method:'POST', headers:{'Content-Type':'application/json'},
            body:JSON.stringify({document_id:this.source.id,theme:this.theme,request_key:this.key})});
          else { const form = new FormData(); form.append('file', this.file); form.append('theme', this.theme); form.append('request_key', this.key);
            result = await this.api('/uploads', {method:'POST',body:form}); }
          await this.select(result); await this.loadRecords();
        });
      },
      async select(job) {
        clearTimeout(this.poll); this.current = job; this.html = '';
        if(this.previewUrl)URL.revokeObjectURL(this.previewUrl); this.previewUrl='';
        if (job.state === 'ready' || job.candidate_available) await this.loadPreview(job.id);
        this.schedulePoll();
      },
      openReader(job) { window.open('/h5-reader.html?job='+encodeURIComponent(job.id), '_blank', 'noopener,noreferrer'); },
      async openRecord(job) { if(job.state==='ready'||job.candidate_available)this.openReader(job);else await this.action(()=>this.select(job)); },
      stateLabel(job) { return ['failed','paused'].includes(job.state) ? '生成已暂停，进度保留' : job.stage; },
      async loadPreview(id) {
        const result = await this.api('/jobs/'+id+'/preview');
        if (this.alive && this.current?.id === id) {
          if(this.previewUrl)URL.revokeObjectURL(this.previewUrl);
          // A blob document keeps #anchors in this document. srcdoc resolves them
          // against the parent URL and can replace the report on navigation.
          this.previewUrl=URL.createObjectURL(new Blob([result.html],{type:'text/html;charset=utf-8'}));
          this.html = result.html;
        }
      },
      schedulePoll() {
        clearTimeout(this.poll);
        if (!this.alive || !this.current) return;
        this.poll = setTimeout(() => this.refresh(), this.current.state === 'ready' ? 30000 : 5000);
      },
      async refresh() {
        if (!this.current || !this.alive) return;
        const id = this.current.id;
        try {
          const result = await this.api('/jobs/'+id);
          if (!this.alive || this.current?.id !== id) return;
          const becameReady = (result.state === 'ready' || result.candidate_available) && (!this.html || result.candidate_key !== this.current.candidate_key);
          this.current = result;
          if (becameReady) { await this.loadPreview(id); await this.loadRecords(); }
          this.error = '';
        } catch(e) { this.error = e.message; this.html = ''; if(this.previewUrl)URL.revokeObjectURL(this.previewUrl);this.previewUrl=''; }
        this.schedulePoll();
      },
      async repairChapter(index) { await this.action(async () => { const result=await this.api('/jobs/'+this.current.id+'/repair-chapter',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chapter:Number(index)})});await this.select(result); }); },
      async resume() { await this.action(async () => { const result = await this.api('/jobs/'+this.current.id+'/resume', {method:'POST'}); await this.select(result); }); },
      async blobDownload(path, filename) {
        const response = await this.request(path);
        if (!response.ok) { const result = await response.json(); throw Error(result.detail || '下载未完成。'); }
        const url = URL.createObjectURL(await response.blob()); const link = document.createElement('a');
        const encoded = /filename\*=UTF-8''([^;]+)/i.exec(response.headers.get('Content-Disposition') || '');
        let suggested = filename; try { if(encoded)suggested=decodeURIComponent(encoded[1]); } catch (_) {}
        link.href = url; link.download = suggested; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      },
      async download(kind) { await this.action(() => this.blobDownload(base+'/jobs/'+this.current.id+'/download?kind='+kind, this.current.title+'.'+kind)); },
      async original() {
        await this.action(() => this.blobDownload(base+'/jobs/'+this.current.id+'/original', this.current.source_name));
      },
      async openSave() {
        await this.action(async () => {
          const response = await this.request('/company-knowledge/projects'); const result = await response.json();
          if (!response.ok) throw Error(result.detail || '目录加载失败。');
          this.projects = result.projects.filter(p => p.can_manage); this.folders = result.folders;
          this.projectId = this.projects.some(p => p.id === this.current.source_project_id) ? this.current.source_project_id : (this.projects[0]?.id || '');
          await this.$nextTick(); this.folderId = this.projectId===this.current.source_project_id && this.currentFolders.some(f=>f.id===this.current.source_folder_id) ? this.current.source_folder_id : ''; this.saveOpen = true;
        });
      },
      async save() {
        await this.action(async () => { this.current = await this.api('/jobs/'+this.current.id+'/save', {method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify({project_id:this.projectId,folder_id:this.folderId})});
          this.saveOpen = false; await this.loadRecords(); });
      },
      openLocation() { this.$emit('location', this.current); },
      date(value) { return value ? new Date(value).toLocaleString('zh-CN') : ''; },
      folderLabel(folder) {
        const parts = [folder.name]; const seen = new Set([folder.id]); let parent = folder.parent_id;
        while (parent && !seen.has(parent)) { seen.add(parent); const item = this.folders.find(f => f.id === parent); if (!item) break; parts.unshift(item.name); parent = item.parent_id; }
        return parts.join(' / ');
      },
    },
    template: `<section class="h5-workbench" aria-label="H5 工作台">
      <header class="h5-intro"><h2>把已有材料变成完整阅读版</h2><p>自动使用文件标题。保留正文、表格、原图和来源；成果可下载或存回知识库。</p></header>
      <p v-if="error" role="alert" class="h5-error">{{ error }}</p>
      <section class="h5-new" aria-label="新建 H5">
        <div class="h5-material"><strong>{{source?.title || file?.name || '选择一份材料'}}</strong><small v-if="file">仅用于 H5 制作，原件不自动进入知识库。</small><small v-else-if="source">来自公司知识库，完成后自动保存到来源目录，沿用权限交集。</small>
          <div class="h5-actions"><el-button :disabled="busy" @click="openChooser">从知识库选择</el-button><el-button :disabled="busy" @click="$refs.fileInput.click()">上传本地文件</el-button><input ref="fileInput" type="file" accept=".pdf,.docx,.txt,.md,.png,.jpg,.jpeg,.webp" hidden @change="chooseFile"></div></div>
        <div class="h5-generate"><label for="h5-theme">主题</label><select id="h5-theme" v-model="theme" :disabled="busy"><option v-if="!themes.length" value="cobalt">Cobalt · 默认主题</option><option v-for="item in themes" :key="item.id" :value="item.id">{{item.name}}</option></select><el-button @click="themeChooser=true" :disabled="busy || !themes.length">看样式</el-button><el-button type="primary" :loading="busy" :disabled="!canGenerate" @click="generate">生成 H5</el-button></div>
      </section>
      <div v-if="selectedTheme" class="h5-selected-theme"><img :src="selectedTheme.preview" :alt="selectedTheme.name+' 主题设计参考'" width="180" height="112"><div><strong>{{selectedTheme.name}} · {{selectedTheme.category}}</strong><p>{{selectedTheme.description}}</p><span class="h5-palette" aria-label="主题配色"><i v-for="color in selectedTheme.palette" :key="color" :style="{background:color}"></i></span><small>主题设计参考，实际内容与排版随材料调整。</small></div></div>
      <el-dialog v-model="themeChooser" title="选择主题样式" width="min(960px,94vw)"><p>点击参考图选择主题，不会发起生成。</p><div class="h5-theme-grid"><button v-for="item in themes" :key="item.id" :aria-pressed="theme===item.id" @click="theme=item.id;themeChooser=false"><img :src="item.preview" :alt="item.name+' 主题设计参考'" loading="lazy" width="320" height="200"><strong>{{item.name}} · {{item.category}}</strong><small>{{item.description}}</small><span class="h5-palette" aria-label="主题配色"><i v-for="color in item.palette" :key="color" :style="{background:color}"></i></span></button></div></el-dialog>
      <section v-if="current" class="h5-current" aria-label="当前成果">
        <header><div><h3>{{current.title}}</h3><p>来源：{{current.source_name}} · {{date(current.created_at)}}</p><p v-if="current.source_changed" class="h5-update">来源内容已更新，可重新选择原文件生成；已有成果保留。</p><p v-if="current.source_kind==='local'">临时任务可恢复至 {{date(current.expires_at)}}；系统无法感知电脑原件的后续变化。</p></div><span class="h5-state" role="status">{{stateLabel(current)}}</span></header>
        <p v-if="Object.keys(current.material_counts || {}).length" class="h5-counts">解析记录 {{current.material_counts.pages}} · 表格 {{current.material_counts.tables}} · 图片 {{current.material_counts.images}}<small>解析记录数不等于原文件实际页数。</small></p>
        <p v-if="current.progress" class="h5-counts">当前阶段：{{current.progress.phase}} · 已完成阅读 {{current.progress.completed_reading || 0}}/{{current.progress.planned_reading || 0}} · 正文草稿 {{current.progress.drafted_chapters || 0}}/{{current.progress.planned_chapters || 0}} · 已核对 {{current.progress.completed_chapters || 0}} · 累计请求 {{current.progress.requests || 0}}</p><p v-if="['failed','paused'].includes(current.state)">{{current.can_resume?'可从检查点恢复':'当前不可恢复，请先检查原因或额度'}} · 已恢复 {{current.resume_count}}/2 次；按剩余章节分配，每次最多 24 次请求、15 分钟。</p><p v-if="current.save_error" class="h5-error" role="alert">{{current.save_error}}</p><p v-if="current.error" class="h5-error" role="alert">{{current.error}}</p>
        <section v-if="current.candidate_available && current.state!=='ready'" aria-label="草稿待核对问题"><p role="status">完整草稿可阅读和下载；待核对标注尚未解决，核对通过后才能保存正式成果。</p><ul><li v-for="(issue,index) in current.pending_chapters" :key="index"><strong>{{Number(index)>=0?'第'+(Number(index)+1)+'章':'报告导语'}}待核对</strong><p>{{issue.reason || '正在自动核对和局部优化'}}</p><el-button v-if="Number(index)>=0 && ['review_pending','failed','paused'].includes(current.state)" :disabled="busy || (current.material_counts.chapter_repair_attempts?.[index] || 0)>=2" @click="repairChapter(index)">仅优化这一章（{{current.material_counts.chapter_repair_attempts?.[index] || 0}}/2）</el-button></li></ul></section>
        <div class="h5-actions"><template v-if="current.state==='ready' || current.candidate_available"><el-button type="primary" @click="openReader(current)">完整阅读</el-button><el-button @click="download('html')" :disabled="busy">下载 HTML</el-button><el-button @click="download('zip')" :disabled="busy">下载网站 ZIP</el-button><el-button v-if="current.state==='ready' && !current.saved_document_id" type="primary" @click="openSave" :disabled="busy">保存到知识库</el-button><el-button v-else-if="current.saved_document_id" @click="openLocation">打开保存位置</el-button><el-button @click="original" :disabled="busy">下载原件</el-button></template><el-button v-if="current.can_resume" @click="resume" :loading="busy">继续已保存进度</el-button><el-button @click="refresh" :disabled="busy">刷新状态</el-button></div>
        <div v-if="html" class="h5-preview-region"><div class="h5-preview-toolbar" role="group" aria-label="预览尺寸"><button :aria-pressed="width==='desktop'" @click="width='desktop'">电脑</button><button :aria-pressed="width==='mobile'" @click="width='mobile'">手机</button><span>原文页内定位可直接使用；原件下载请使用上方入口。</span></div><div class="h5-frame-wrap"><iframe title="H5 成果隔离预览" sandbox="allow-scripts" referrerpolicy="no-referrer" :class="{'h5-frame-mobile':width==='mobile'}" :src="previewUrl"></iframe></div></div>
      </section>
      <section class="h5-records" aria-label="生成记录"><header><h3>生成记录</h3><el-button text @click="action(loadRecords)" :disabled="busy">刷新记录</el-button></header><p v-if="!records.length">还没有制作记录。</p><ul><li v-for="item in records" :key="item.id"><button @click="openRecord(item)"><strong>{{item.title}}</strong><small>{{item.source_name}} · {{date(item.created_at)}}</small></button><span>{{stateLabel(item)}}</span><small v-if="item.state==='ready'">{{item.saved_document_id?'已保存到知识库':'未保存到知识库'}}</small></li></ul><div class="h5-pagination"><el-button :disabled="recordPage<=1||busy" @click="recordPage--;action(loadRecords)">上一页</el-button><span>{{recordPage}} / {{Math.max(1,Math.ceil(recordTotal/20))}}</span><el-button :disabled="recordPage*20>=recordTotal||busy" @click="recordPage++;action(loadRecords)">下一页</el-button></div></section>
      <el-dialog v-model="chooser" title="从知识库选择一份文件" width="min(680px,94vw)"><form class="h5-actions" @submit.prevent="sourcePage=1;action(loadSources)"><el-input aria-label="搜索材料名称" v-model="query" placeholder="搜索文件名"></el-input><el-button native-type="submit" :loading="busy">搜索</el-button></form><ul class="h5-source-list"><li v-for="item in sources" :key="item.id"><button @click="chooseSource(item)"><strong>{{item.title}}</strong><small>{{item.format}} · {{item.project || '知识库'}} · {{Math.ceil(item.size/1024)}} KB</small></button></li></ul><p v-if="!sources.length">没有可读取且有原件的受支持文件。</p><div class="h5-pagination"><el-button :disabled="sourcePage<=1||busy" @click="sourcePage--;action(loadSources)">上一页</el-button><span>{{sourcePage}}</span><el-button :disabled="sourcePage*20>=sourceTotal||busy" @click="sourcePage++;action(loadSources)">下一页</el-button></div></el-dialog>
      <el-dialog v-model="saveOpen" title="保存 H5 展示成果" width="min(540px,94vw)"><p>{{current?.title}} · 无需问答索引</p><label class="h5-field">项目<select v-model="projectId"><option v-for="item in projects" :key="item.id" :value="item.id">{{item.name}}</option></select></label><label class="h5-field">目录<select v-model="folderId"><option value="">项目根目录</option><option v-for="item in currentFolders" :key="item.id" :value="item.id">{{folderLabel(item)}}</option></select></label><p>{{current?.source_kind==='local'?'本地材料成果仅本人和管理员可见。':'成果可见范围为来源权限与目标目录权限的交集，来源权限收回后不能继续访问。'}}</p><p v-if="!projects.length" role="status">当前没有可写入的项目目录，仍可直接下载。</p><template #footer><el-button @click="saveOpen=false">取消</el-button><el-button type="primary" :loading="busy" :disabled="!projectId" @click="save">保存</el-button></template></el-dialog>
    </section>`,
  };
  function install(options) {
    options.components = {...options.components, 'h5-workbench':component};
    const title = options.computed.pageTitle, switchView = options.methods.switchView;
    options.computed.pageTitle = function() { return this.activeView==='knowledge-h5' ? 'H5 制作' : title.call(this); };
    options.methods.switchView = function(view, ...args) {
      if (view !== 'knowledge-h5') return switchView.call(this, view, ...args);
      if (!this.h5Enabled) return;
      if (this.$refs.knowledgeWorkspace?.busy) return;
      this.activeView=view; this.mobileNavOpen=false; this.taskCreateOpen=false;
    };
    options.mixins = [...(options.mixins || []), {
      data() { return {h5Enabled:false,h5Entry:null}; },
      watch: { token: {immediate:true, async handler(value) {
        this.h5Enabled=false; this.h5Entry=null;
        if (!value) return;
        try { const response=await this.authFetch('/company-knowledge/status');const data=await response.json(); if(this.token===value)this.h5Enabled=!!data.h5_enabled && !!data.access_allowed; } catch (_) {}
      }}},
      methods: {
        openH5(doc) { if(!this.h5Enabled)return;this.h5Entry={kind:'generate',document:doc,sequence:Date.now()};this.switchView('knowledge-h5'); },
        openSavedH5(doc) { if(!this.h5Enabled)return;this.h5Entry={kind:'saved',id:doc.id,sequence:Date.now()};this.switchView('knowledge-h5'); },
        async openH5Location(job) { this.switchView('company-knowledge'); await this.$nextTick(); const workspace=this.$refs.knowledgeWorkspace;if(!workspace)return;await workspace.loadProjectDirectories();workspace.openDocumentDirectory({knowledge_project_id:job.saved_project_id,folder_id:job.saved_folder_id,category:job.saved_category}); },
      },
    }];
  }
  return {install, component, supported};
})();
if (typeof module !== 'undefined') module.exports = H5Workbench;
