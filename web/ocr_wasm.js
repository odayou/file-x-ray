// 浏览器内置 OCR（tesseract.js WASM）：随 exe 分发的零安装兜底引擎。
// 定位与 Edge 的端侧 OCR 思路一致——模型随程序分发、在渲染端运行：
//   · 语言包与 wasm 核心在 /web/vendor/tesseract/（由后端静态伺服、随包离线可用）
//   · 仅处理扫描件 PDF（图片 OCR 仍走后端引擎 Umi-OCR/tesseract）
//   · 识别结果通过 PUT /api/file/{id} 回传入库，复用既有的待识别池/徽标/校对闭环
const WasmOcr = (() => {
  const VENDOR = '/web/vendor/tesseract';
  const LANGS = 'chi_sim+eng';
  let workerPromise = null;      // 惰性创建，整个页面生命周期复用一个 worker

  // 文件是否就绪以后端探测为准（/api/ocr/check 的 wasm_available）
  function available(){
    return !!(state.ocr && state.ocr.wasm_available);
  }

  function loadScript(src){
    return new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = src; s.onload = res;
      s.onerror = () => rej(new Error('加载失败: ' + src));
      document.head.appendChild(s);
    });
  }

  async function ensureWorker(){
    if(!workerPromise){
      workerPromise = (async () => {
        await loadScript(VENDOR + '/tesseract.min.js');
        return Tesseract.createWorker(LANGS, 1, {   // 1 = LSTM_ONLY
          workerPath: VENDOR + '/worker.min.js',
          corePath: VENDOR,        // 目录：worker 按 SIMD 支持自动挑选核心
          langPath: VENDOR,        // chi_sim/eng traineddata.gz 所在目录
          logger: () => {}         // 页面级进度由调用方负责
        });
      })();
      workerPromise.catch(() => { workerPromise = null; });   // 失败允许重试
    }
    return workerPromise;
  }

  async function fetchBinary(url){
    const t = window.__FIT_AUTH_TOKEN__ || '';
    const u = t ? url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(t) : url;
    const r = await fetch(u);
    if(!r.ok) throw new Error('HTTP ' + r.status);
    return await r.blob();
  }

  // 识别整个 PDF：逐页渲染 → OCR → 增量回传（中途关页面也不丢已识别内容）
  // onPage(page, pages)：进度回调；返回拼接后的全文。
  async function ocrPdf(fid, onPage){
    const worker = await ensureWorker();
    const meta = await apiGet('/api/pdf/' + fid + '/pages');
    const parts = [];
    for(let i = 1; i <= meta.pages; i++){
      const blob = await fetchBinary('/api/pdf/' + fid + '/page/' + i + '?dpi=200');
      const r = await worker.recognize(blob);
      const t = ((r.data && r.data.text) || '').trim();
      if(t) parts.push(t);
      if(onPage) onPage(i, meta.pages);
      await apiPut('/api/file/' + fid,
                   { pdf_text: parts.join('\n'), pdf_text_source: 'ocr' });
    }
    return parts.join('\n');
  }

  return { available, ocrPdf };
})();
