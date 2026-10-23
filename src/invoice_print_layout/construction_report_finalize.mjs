/** Packaged finalizer adapter and first-party import check. Never launches Office. */
import fs from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';

const [specPath, mode] = process.argv.slice(2);
const spec = JSON.parse((await fs.readFile(specPath, 'utf8')).replace(/^\uFEFF/, ''));
const options = spec.options;
const hash = async p => createHash('sha256').update(await fs.readFile(p)).digest('hex');
if (mode === 'import') {
  const before = await hash(options.candidatePath);
  const {FileBlob, PresentationFile} = await import(pathToFileURL(spec.runtime.artifact_module).href);
  const deck = await PresentationFile.importPptx(await FileBlob.load(options.candidatePath));
  const after = await hash(options.candidatePath);
  if (before !== after || deck.slides.items.length !== options.explicitTotalSlideCount) throw new Error('Import changed bytes or slide count');
  console.log(JSON.stringify({passed: true, slideCount: deck.slides.items.length, sha256: after}));
} else if (mode === 'finalize') {
  try {
    const {finalizePresentation} = await import(pathToFileURL(path.join(spec.runtime.skill_dir, 'container_tools/artifact_tool_utils.mjs')).href);
    await finalizePresentation(options);
    await fs.writeFile(spec.status_path, JSON.stringify({passed: true, finalizer_returned: true}));
    console.log(JSON.stringify({passed: true, finalizer_returned: true}));
  } catch (error) {
    const spawnError = [error, error.cause].find(e => e?.code === 'EBUSY' && /^spawn(?:Sync)?\b/.test(e?.syscall ?? ''));
    await fs.writeFile(spec.status_path, JSON.stringify({passed: false, finalizer_returned: false,
      failure_kind: spawnError ? 'child_process_ebusy' : 'other',
      error: {message: error.message, code: error.code, validator: error.validator,
        cause_code: error.cause?.code, cause_syscall: error.cause?.syscall}}, null, 2));
    console.error(error.stack ?? String(error));
    process.exitCode = 1;
  }
} else {
  throw new Error('Expected finalize or import mode');
}
