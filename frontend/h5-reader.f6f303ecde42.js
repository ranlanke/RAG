/* The shell is trusted; generated content only enters an opaque-origin frame. */
(async () => {
  const status = document.getElementById('status'), frame = document.getElementById('report');
  let url = '';
  try {
    const id = new URL(location.href).searchParams.get('job');
    if (!/^[a-f0-9]{32}$/.test(id || '')) throw Error('成果地址无效。');
    const token = localStorage.getItem('accessToken');
    if (!token) throw Error('请先在系统首页登录，再打开成果。');
    const base = '/company-knowledge/h5/jobs/' + id;
    const read = async path => {
      const response = await fetch(path, {headers:{Authorization:'Bearer '+token},cache:'no-store'});
      const data = await response.json();
      if (!response.ok) throw Error(response.status===401 ? '登录已失效，请返回系统重新登录。' : (data.detail || '成果不可访问。'));
      return data;
    };
    const job = await read(base);
    if (job.state !== 'ready' && !job.candidate_available) throw Error('完整成果尚未生成，请在 H5 制作中查看进度。');
    document.title = job.title; document.getElementById('title').textContent = job.title;
    const result = await read(base+'/preview?full=true');
    url = URL.createObjectURL(new Blob([result.html],{type:'text/html;charset=utf-8'}));
    frame.src = url; frame.hidden = false; status.hidden = true;
  } catch (error) { status.textContent = error.message; }
  addEventListener('pagehide', () => {if(url)URL.revokeObjectURL(url);});
})();
