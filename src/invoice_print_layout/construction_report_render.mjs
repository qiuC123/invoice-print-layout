/** Render one private construction-report attempt. All project facts live in job.json. */
import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

function requiredPath(value, label) {
  if (typeof value !== 'string' || !path.isAbsolute(value)) {
    throw new Error(`${label} must be an absolute path`);
  }
  return value;
}

function positiveInteger(value, label) {
  if (!Number.isSafeInteger(value) || value < 1) throw new Error(`Invalid ${label}`);
}

function readRecords(snapshot) {
  return snapshot.ndjson.split(/\r?\n/).filter(Boolean).map(line => JSON.parse(line));
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character]);
}

async function main() {
  const jobPath = requiredPath(process.argv[2], 'job.json');
  const mode = process.argv[3];
  if (!['edit', 'preview'].includes(mode)) throw new Error('Expected edit or preview mode; use the daily CLI to orchestrate the complete run');
  const job = JSON.parse((await fs.readFile(jobPath, 'utf8')).replace(/^\uFEFF/, ''));
  for (const key of ['stage_template', 'base_pptx', 'run_dir', 'artifact_module', 'skill_dir', 'python_executable']) {
    requiredPath(job[key], key);
  }
  for (const key of ['expected_base_slides', 'insert_after', 'block_start', 'block_end', 'expected_slide_count']) {
    positiveInteger(job[key], key);
  }
  if (typeof job.drop_blank_last !== 'boolean') throw new Error('drop_blank_last must be boolean');
  const blockCount = job.block_end - job.block_start + 1;
  if (blockCount < 1 || job.insert_after > job.expected_base_slides - Number(job.drop_blank_last)) {
    throw new Error('Invalid selected block or insertion point');
  }
  if (job.expected_slide_count !== job.expected_base_slides - Number(job.drop_blank_last) + blockCount) {
    throw new Error('Expected slide count does not match composition');
  }
  for (const key of ['edits', 'texts', 'images', 'cover_replacements']) {
    if (!Array.isArray(job[key])) throw new Error(`${key} must be an array`);
  }
  const buildDir = path.join(job.run_dir, 'build');
  const outputDir = path.join(job.run_dir, 'output');
  await fs.mkdir(buildDir, { recursive: true });
  await fs.mkdir(outputDir, { recursive: true });
  if (mode === 'preview') {
    await writePreview(job, outputDir);
    return;
  }
  for (const existing of [path.join(buildDir, 'stage-edited.pptx'), path.join(buildDir, 'candidate.pptx'), path.join(outputDir, 'report.pptx')]) {
    try {
      await fs.stat(existing);
    } catch (error) {
      if (error.code === 'ENOENT') continue;
      throw error;
    }
    throw new Error('Attempt already contains a render; use a new attempt directory');
  }
  const { FileBlob, PresentationFile } = await import(pathToFileURL(job.artifact_module).href);
  const presentation = await PresentationFile.importPptx(await FileBlob.load(job.stage_template));
  if (job.block_end > presentation.slides.items.length) throw new Error('Selected block exceeds stage template');
  const before = await presentation.inspect({ kind: 'slide,textbox,shape,image,layout', maxChars: 2000000 });
  await fs.writeFile(path.join(buildDir, 'stage-before.ndjson'), before.ndjson);
  const records = readRecords(before);
  const expectedTexts = new Map();
  function resolveSelected(id, kind) {
    const record = records.find(item => item.id === id && (kind === 'image' ? item.kind === 'image' : typeof item.text === 'string'));
    if (!record || record.slide < job.block_start || record.slide > job.block_end) {
      throw new Error(`Anchor ${id} is missing or outside the selected ${kind} block`);
    }
    return { target: presentation.resolve(id), record };
  }
  for (const edit of job.edits) {
    if (typeof edit.old !== 'string' || !edit.old || typeof edit.text !== 'string') throw new Error('Invalid text edit');
    const { target, record } = resolveSelected(edit.id, 'text');
    const previous = expectedTexts.get(edit.id) ?? record.text;
    if (previous.split(edit.old).length !== 2) throw new Error(`Text anchor ${edit.id} must have exactly one old-text match`);
    target.text.replace(edit.old, edit.text);
    expectedTexts.set(edit.id, previous.replace(edit.old, () => edit.text));
  }
  for (const edit of job.texts) {
    if (typeof edit.text !== 'string') throw new Error('Invalid full text replacement');
    const { target } = resolveSelected(edit.id, 'text');
    target.text = edit.text;
    expectedTexts.set(edit.id, edit.text);
  }
  const imageIds = new Set();
  for (const edit of job.images) {
    requiredPath(edit.path, 'image path');
    if (imageIds.has(edit.id)) throw new Error(`Duplicate image anchor ${edit.id}`);
    imageIds.add(edit.id);
    const { target } = resolveSelected(edit.id, 'image');
    const frame = { ...target.frame };
    const contentType = ({ '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg' })[path.extname(edit.path).toLowerCase()];
    if (!contentType) throw new Error('Report images must be PNG or JPEG');
    await target.replace({ blob: new Uint8Array(await fs.readFile(edit.path)), contentType, fit: 'contain' });
    target.frame = frame;
  }
  for (const slide of presentation.slides.items) slide.speakerNotes.textFrame.setText('');
  const after = await presentation.inspect({ kind: 'slide,textbox,shape,image', maxChars: 2000000 });
  await fs.writeFile(path.join(buildDir, 'stage-after.ndjson'), after.ndjson);
  const afterRecords = readRecords(after);
  for (const [id, expected] of expectedTexts) {
    const actual = afterRecords.find(item => item.id === id && typeof item.text === 'string');
    if (!actual || actual.text !== expected) throw new Error(`Edited text did not round-trip at ${id}`);
  }
  await (await PresentationFile.exportPptx(presentation)).save(path.join(buildDir, 'stage-edited.pptx'));
  console.log(JSON.stringify({ status: 'stage-edited', pptx: path.join(buildDir, 'stage-edited.pptx') }));
}

async function writePreview(job, outputDir) {
  const title = escapeHtml(job.report_title ?? '施工日报');
  const pages = Array.from({ length: job.expected_slide_count }, (_, index) => {
    const number = index + 1;
    return `<figure><img src="pages/slide-${String(number).padStart(3, '0')}.png" alt="第 ${number} 页" loading="lazy"><figcaption>${number} / ${job.expected_slide_count}</figcaption></figure>`;
  }).join('\n');
  const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${title}</title><style>body{margin:0;background:#e9edf0;color:#17212b;font-family:system-ui,sans-serif}header{max-width:1280px;margin:24px auto;padding:0 16px}h1{font-size:24px}a{color:#135a9c}main{max-width:1280px;margin:auto;padding:0 16px}figure{margin:0 0 24px}img{display:block;width:100%;height:auto;background:white}figcaption{text-align:center;padding:8px;font-size:14px}@media print{header,figcaption{display:none}main{max-width:none;padding:0}figure{break-after:page;margin:0}}</style></head><body><header><h1>${title}</h1><a href="report.pptx">下载可编辑 PowerPoint</a></header><main>${pages}</main></body></html>`;
  await fs.writeFile(path.join(outputDir, 'preview.html'), html, { flag: 'wx' });
  console.log(JSON.stringify({ status: 'verified', pptx: path.join(outputDir, 'report.pptx'), preview: path.join(outputDir, 'preview.html') }));
}

main().catch(error => {
  console.error(error.stack ?? String(error));
  process.exitCode = 1;
});
