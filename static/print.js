/* Renders one saved note as a clean printable sheet.

   Used two ways: opened in a tab so the browser's own print dialog can save it
   as a PDF, and loaded by headless Chromium server-side for a one-click
   download. Both wait on `document.body.dataset.ready`, which is only set once
   the markdown is rendered, the math is typeset, and every image has settled -
   otherwise a PDF can be captured with half the pages blank. */

(function () {
  'use strict';

  function fail(message) {
    document.getElementById('sheet').innerHTML =
      '<p class="loading error"></p>';
    document.querySelector('.loading').textContent = message;
    document.body.dataset.ready = 'error';
  }

  function resolveSavedImage(src) {
    return src.replace(/^(\.\.\/)?assets\//, '/assets/');
  }

  function typeset(root) {
    if (!window.katex) return;
    root.querySelectorAll('.math').forEach(function (node) {
      try {
        window.katex.render(node.textContent, node, {
          displayMode: node.classList.contains('math-display'),
          throwOnError: false,
          errorColor: '#a3271f',
          trust: false,
          strict: 'ignore',
        });
      } catch (error) {
        node.classList.add('math-failed');
      }
    });
  }

  // Resolve once every image has either loaded or failed - never hang on one
  // broken asset, and never let the PDF capture race ahead of the images.
  function imagesSettled(root) {
    const images = Array.prototype.slice.call(root.querySelectorAll('img'));
    return Promise.all(images.map(function (img) {
      if (img.complete) return Promise.resolve();
      return new Promise(function (resolve) {
        img.addEventListener('load', resolve, { once: true });
        img.addEventListener('error', resolve, { once: true });
      });
    }));
  }

  async function main() {
    const params = new URLSearchParams(window.location.search);
    const slug = params.get('slug');
    if (!slug) return fail('No note was specified.');

    let note;
    try {
      const response = await fetch('/api/notes/' + encodeURIComponent(slug));
      if (!response.ok) {
        const body = await response.json().catch(function () { return {}; });
        throw new Error(body.detail || response.statusText);
      }
      note = await response.json();
    } catch (error) {
      return fail('Could not load that note: ' + (error.message || error));
    }

    const result = window.NoteMarkdown.render(note.markdown, {
      resolveImage: resolveSavedImage,
    });

    document.title = note.title || slug;
    const sheet = document.getElementById('sheet');
    sheet.innerHTML = '';

    const meta = document.createElement('div');
    meta.className = 'sheet-meta';
    meta.textContent = [note.subject, note.date].filter(Boolean).join(' · ');
    if (meta.textContent) sheet.appendChild(meta);

    const body = document.createElement('article');
    body.className = 'markdown';
    body.innerHTML = result.html;
    sheet.appendChild(body);

    typeset(body);
    await imagesSettled(body);

    document.body.dataset.ready = 'true';
    if (params.get('print') === '1') window.print();
  }

  main();
})();
