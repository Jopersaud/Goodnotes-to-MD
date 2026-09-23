/* GoodNotes -> Markdown: front-end. Vanilla JS, no build step. */

(function () {
  'use strict';

  const $ = function (id) { return document.getElementById(id); };

  const state = {
    config: null,
    pages: [],          // { file, url, name }
    result: null,       // last conversion payload
    previewMap: {},     // "page1.png" -> upload URL
    dragIndex: null,
    converting: false,
    openNote: null,
    editing: false,
  };

  /* ----------------------------- utilities ----------------------------- */

  function show(el, visible) { el.hidden = !visible; }

  function banner(el, message, detail) {
    if (!message) { show(el, false); return; }
    el.innerHTML = '';
    const strong = document.createElement('strong');
    strong.textContent = message;
    el.appendChild(strong);
    if (detail) {
      const pre = document.createElement('pre');
      pre.textContent = detail;
      el.appendChild(pre);
    }
    show(el, true);
  }

  function formatDate(value) {
    if (!value) return '';
    const parsed = new Date(value);
    if (isNaN(parsed.getTime())) return value;
    return parsed.toLocaleDateString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
    });
  }

  function isoDate(value) {
    return /^\d{4}-\d{2}-\d{2}$/.test(value || '') ? value : '';
  }

  async function api(path, options) {
    const response = await fetch(path, options);
    let body = null;
    try { body = await response.json(); } catch (err) { body = null; }
    if (!response.ok) {
      const detail = (body && (body.detail || body.message)) || response.statusText;
      throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    }
    return body;
  }

  /* --------------------------- image resolution -------------------------- */

  // Markdown links look like ../assets/<slug>/page1.png. Before a note is
  // saved those files only exist in the upload session, so map by basename.
  function resolvePreviewImage(src) {
    const name = src.split('/').pop();
    if (state.previewMap[name]) return state.previewMap[name];
    return src.replace(/^(\.\.\/)?assets\//, '/assets/');
  }

  function resolveSavedImage(src) {
    return src.replace(/^(\.\.\/)?assets\//, '/assets/');
  }

  function renderInto(el, markdown, resolver) {
    const result = window.NoteMarkdown.render(markdown, { resolveImage: resolver });
    el.innerHTML = '';
    if (result.meta) {
      const row = document.createElement('div');
      row.className = 'frontmatter';
      [
        result.meta.subject,
        formatDate(result.meta.date),
        (result.meta.source_images || []).length
          ? (result.meta.source_images || []).length + ' page(s)'
          : '',
      ].filter(Boolean).forEach(function (text) {
        const chip = document.createElement('span');
        chip.className = 'chip';
        chip.textContent = text;
        row.appendChild(chip);
      });
      if (row.childNodes.length) el.appendChild(row);
    }
    const body = document.createElement('div');
    body.innerHTML = result.html;
    el.appendChild(body);
  }

  /* ------------------------------- tabs -------------------------------- */

  function switchTab(name) {
    const isConvert = name === 'convert';
    show($('view-convert'), isConvert);
    show($('view-library'), !isConvert);
    $('tab-convert').classList.toggle('active', isConvert);
    $('tab-library').classList.toggle('active', !isConvert);
    if (!isConvert) loadNotes();
  }

  /* ------------------------------ pages -------------------------------- */

  function addFiles(fileList) {
    const allowed = (state.config && state.config.allowed_suffixes) || ['.png', '.jpg'];
    const rejected = [];
    Array.prototype.forEach.call(fileList, function (file) {
      const dot = file.name.lastIndexOf('.');
      const suffix = dot === -1 ? '' : file.name.slice(dot).toLowerCase();
      if (allowed.indexOf(suffix) === -1) { rejected.push(file.name); return; }
      state.pages.push({ file: file, url: URL.createObjectURL(file), name: file.name });
    });
    // GoodNotes exports as IMG_1234.PNG, so name order is usually page order.
    state.pages.sort(function (a, b) {
      return a.name.localeCompare(b.name, undefined, { numeric: true });
    });
    if (rejected.length) {
      banner(
        $('convert-error'),
        'Skipped ' + rejected.length + ' file(s) that are not supported images.',
        rejected.join('\n') + '\n\nSupported: ' + allowed.join(', ')
      );
    }
    renderThumbs();
  }

  function removePage(index) {
    URL.revokeObjectURL(state.pages[index].url);
    state.pages.splice(index, 1);
    renderThumbs();
  }

  function clearPages() {
    state.pages.forEach(function (page) { URL.revokeObjectURL(page.url); });
    state.pages = [];
    state.result = null;
    state.previewMap = {};
    show($('result'), false);
    show($('progress'), false);
    banner($('convert-error'), null);
    renderThumbs();
    $('file-input').value = '';
  }

  function renderThumbs() {
    const host = $('thumbs');
    host.innerHTML = '';
    state.pages.forEach(function (page, index) {
      const card = document.createElement('div');
      card.className = 'thumb';
      card.draggable = true;
      card.dataset.index = String(index);

      const img = document.createElement('img');
      img.src = page.url;
      img.alt = page.name;
      card.appendChild(img);

      const meta = document.createElement('div');
      meta.className = 'meta';
      const pageLabel = document.createElement('span');
      pageLabel.className = 'page';
      pageLabel.textContent = 'p' + (index + 1);
      const name = document.createElement('span');
      name.className = 'name';
      name.textContent = page.name;
      name.title = page.name;
      const remove = document.createElement('button');
      remove.className = 'remove';
      remove.textContent = '×';
      remove.title = 'Remove this page';
      remove.addEventListener('click', function (event) {
        event.stopPropagation();
        removePage(index);
      });
      meta.append(pageLabel, name, remove);
      card.appendChild(meta);

      card.addEventListener('dragstart', function () {
        state.dragIndex = index;
        card.classList.add('dragging');
      });
      card.addEventListener('dragend', function () {
        state.dragIndex = null;
        card.classList.remove('dragging');
        Array.prototype.forEach.call(
          host.children,
          function (child) { child.classList.remove('over'); }
        );
      });
      card.addEventListener('dragover', function (event) {
        event.preventDefault();
        if (state.dragIndex !== null && state.dragIndex !== index) {
          card.classList.add('over');
        }
      });
      card.addEventListener('dragleave', function () { card.classList.remove('over'); });
      card.addEventListener('drop', function (event) {
        event.preventDefault();
        event.stopPropagation();
        const from = state.dragIndex;
        if (from === null || from === index) return;
        const moved = state.pages.splice(from, 1)[0];
        state.pages.splice(index, 0, moved);
        renderThumbs();
      });

      host.appendChild(card);
    });

    const count = state.pages.length;
    $('page-count').textContent = count
      ? count + (count === 1 ? ' page' : ' pages') + ' — drag to reorder'
      : '';
    $('convert').disabled = count === 0 || state.converting;
    $('clear').disabled = count === 0 || state.converting;

    const threshold = (state.config && state.config.page_warn_threshold) || 15;
    if (count > threshold) {
      banner(
        $('page-warning'),
        count + ' pages is a lot for one note.',
        'These are all sent to Claude as pages of a single note. If some belong '
        + 'to a different note, remove them and convert separately.'
      );
    } else {
      show($('page-warning'), false);
    }
  }

  /* ----------------------------- conversion ---------------------------- */

  function logProgress(kind, message, replaceLast) {
    const host = $('progress');
    show(host, true);
    if (replaceLast && host.lastChild && host.lastChild.dataset.kind === kind) {
      host.lastChild.textContent = message;
    } else {
      const line = document.createElement('div');
      line.className = 'line ' + kind;
      line.dataset.kind = kind;
      line.textContent = message;
      host.appendChild(line);
    }
    host.scrollTop = host.scrollHeight;
  }

  async function convert() {
    if (state.converting || !state.pages.length) return;
    state.converting = true;
    $('convert').disabled = true;
    $('clear').disabled = true;
    $('regenerate').disabled = true;
    banner($('convert-error'), null);
    banner($('save-status'), null);
    show($('result'), false);
    $('progress').innerHTML = '';
    logProgress('status', 'Uploading ' + state.pages.length + ' page(s)…');

    const form = new FormData();
    state.pages.forEach(function (page) { form.append('files', page.file, page.name); });
    const model = encodeURIComponent($('model').value || '');

    try {
      const response = await fetch('/api/convert?model=' + model, {
        method: 'POST',
        body: form,
      });
      if (!response.ok || !response.body) {
        let detail = response.statusText;
        try {
          const body = await response.json();
          if (body && body.detail) detail = body.detail;
        } catch (err) { /* keep statusText */ }
        throw new Error(detail);
      }
      await readEventStream(response.body);
    } catch (error) {
      banner($('convert-error'), 'Conversion failed.', String(error.message || error));
    } finally {
      state.converting = false;
      $('regenerate').disabled = false;
      renderThumbs();
    }
  }

  async function readEventStream(body) {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });

      let split = buffer.indexOf('\n\n');
      while (split !== -1) {
        handleEvent(buffer.slice(0, split));
        buffer = buffer.slice(split + 2);
        split = buffer.indexOf('\n\n');
      }
    }
  }

  function handleEvent(raw) {
    let name = 'message';
    const data = [];
    raw.split('\n').forEach(function (line) {
      if (line.startsWith('event:')) name = line.slice(6).trim();
      else if (line.startsWith('data:')) data.push(line.slice(5).trim());
    });
    if (!data.length) return;

    let payload;
    try { payload = JSON.parse(data.join('\n')); } catch (err) { return; }

    if (name === 'progress') {
      logProgress(payload.kind || 'status', payload.message, payload.kind === 'writing');
    } else if (name === 'error') {
      banner($('convert-error'), payload.message, payload.detail);
      logProgress('warning', 'Stopped.');
    } else if (name === 'done') {
      logProgress('done', 'Done.');
      showResult(payload);
    }
  }

  function showResult(payload) {
    state.result = payload;
    state.previewMap = {};
    (payload.pages || []).forEach(function (page) {
      state.previewMap[page.asset_name] = page.url;
    });

    $('out-title').value = payload.title || '';
    $('out-subject').value = payload.subject || '';
    $('out-date').value = isoDate(payload.date);
    $('out-markdown').value = payload.markdown || '';

    const bits = [];
    bits.push((payload.pages || []).length + ' page(s)');
    bits.push(
      (payload.diagram_pages || []).length
        ? 'diagrams on page ' + payload.diagram_pages.join(', ')
        : 'no diagrams detected'
    );
    bits.push(payload.exercise_count + ' exercise(s)');
    if (!payload.date_guess) bits.push('no date in the note, using today');
    bits.push('via ' + payload.model);
    $('result-summary').textContent = bits.join(' · ');

    show($('result'), true);
    renderPreview();
  }

  function renderPreview() {
    renderInto($('out-render'), $('out-markdown').value, resolvePreviewImage);
  }

  // Keep the front matter and the H1 in step with the fields above the preview.
  function applyMetaFields() {
    let markdown = $('out-markdown').value;
    const title = $('out-title').value.trim();
    const subject = $('out-subject').value.trim();
    const date = $('out-date').value.trim();

    function setFrontMatter(key, value) {
      const pattern = new RegExp('^(' + key + ':).*$', 'm');
      const quoted = /^\d{4}-\d{2}-\d{2}$/.test(value)
        ? value
        : '"' + value.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
      if (pattern.test(markdown)) markdown = markdown.replace(pattern, '$1 ' + quoted);
    }

    setFrontMatter('title', title);
    setFrontMatter('subject', subject);
    setFrontMatter('date', date);
    markdown = markdown.replace(/^#\s+.*$/m, '# ' + title);

    $('out-markdown').value = markdown;
    renderPreview();
  }

  async function saveNote() {
    if (!state.result) return;
    $('save').disabled = true;
    try {
      const info = await api('/api/notes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: state.result.session_id,
          markdown: $('out-markdown').value,
          title: $('out-title').value.trim() || 'Untitled note',
          subject: $('out-subject').value.trim(),
          date: $('out-date').value.trim(),
        }),
      });
      banner(
        $('save-status'),
        'Saved as ' + info.filename,
        info.path + '\nScreenshots: ' + (state.config.assets_dir + '/' + info.slug)
      );
      state.result = null;
      clearPages();
      show($('save-status'), true);   // lives outside the preview card
      loadNotes();
    } catch (error) {
      banner($('convert-error'), 'Could not save this note.', String(error.message || error));
    } finally {
      $('save').disabled = false;
    }
  }

  /* ------------------------------ library ------------------------------ */

  async function loadNotes() {
    let notes = [];
    try {
      const body = await api('/api/notes');
      notes = body.notes || [];
    } catch (error) {
      notes = [];
    }
    $('tab-library').textContent = notes.length ? 'Library (' + notes.length + ')' : 'Library';

    const host = $('notes-list');
    host.innerHTML = '';
    show($('notes-empty'), notes.length === 0);
    $('notes-dir').textContent = state.config ? 'Notes folder: ' + state.config.notes_dir : '';

    notes.forEach(function (note) {
      const row = document.createElement('div');
      row.className = 'note-row';
      row.addEventListener('click', function () { openNote(note.slug); });

      const left = document.createElement('div');
      const title = document.createElement('div');
      title.className = 'title';
      title.textContent = note.title;
      const detail = document.createElement('div');
      detail.className = 'detail';
      detail.textContent = [
        note.subject,
        formatDate(note.date),
        note.page_count + ' page(s)',
      ].filter(Boolean).join(' · ');
      left.append(title, detail);

      const right = document.createElement('div');
      right.className = 'right';
      const open = document.createElement('span');
      open.className = 'detail';
      open.textContent = note.filename;
      right.appendChild(open);

      row.append(left, right);
      host.appendChild(row);
    });
  }

  async function openNote(slug) {
    try {
      const note = await api('/api/notes/' + encodeURIComponent(slug));
      state.openNote = note;
      state.editing = false;
      show($('library-list'), false);
      show($('library-detail'), true);
      banner($('note-status'), null);
      $('note-path').textContent = note.path;
      $('note-editor').value = note.markdown;
      setEditing(false);
      renderInto($('note-render'), note.markdown, resolveSavedImage);
    } catch (error) {
      banner($('note-status'), 'Could not open that note.', String(error.message || error));
    }
  }

  function setEditing(editing) {
    state.editing = editing;
    show($('note-editor'), editing);
    show($('note-render'), !editing);
    show($('note-save'), editing);
    show($('note-cancel'), editing);
    show($('note-edit'), !editing);
  }

  async function saveOpenNote() {
    if (!state.openNote) return;
    try {
      await api('/api/notes/' + encodeURIComponent(state.openNote.slug), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ markdown: $('note-editor').value }),
      });
      state.openNote.markdown = $('note-editor').value;
      renderInto($('note-render'), state.openNote.markdown, resolveSavedImage);
      setEditing(false);
      banner($('note-status'), 'Changes saved.');
      loadNotes();
    } catch (error) {
      banner($('note-status'), 'Could not save changes.', String(error.message || error));
    }
  }

  async function deleteOpenNote() {
    if (!state.openNote) return;
    const ok = window.confirm(
      'Delete "' + state.openNote.title + '"?\n\nThis removes the .md file and its '
      + 'screenshots folder. It cannot be undone.'
    );
    if (!ok) return;
    try {
      await api('/api/notes/' + encodeURIComponent(state.openNote.slug), { method: 'DELETE' });
      state.openNote = null;
      show($('library-detail'), false);
      show($('library-list'), true);
      loadNotes();
    } catch (error) {
      banner($('note-status'), 'Could not delete that note.', String(error.message || error));
    }
  }

  /* -------------------------------- init ------------------------------- */

  async function init() {
    try {
      state.config = await api('/api/config');
    } catch (error) {
      state.config = { models: [{ id: 'sonnet', label: 'sonnet' }], default_model: 'sonnet' };
    }

    const select = $('model');
    (state.config.models || []).forEach(function (model) {
      const option = document.createElement('option');
      option.value = model.id;
      option.textContent = model.label;
      if (model.id === state.config.default_model) option.selected = true;
      select.appendChild(option);
    });

    const notices = state.config.env_notices || [];
    if (notices.length) {
      const stripped = notices.filter(function (n) { return n.action === 'stripped'; });
      const kept = notices.filter(function (n) { return n.action === 'kept'; });
      const lines = [];
      if (stripped.length) {
        lines.push(
          stripped.map(function (n) { return n.var; }).join(', ')
          + ' is set here and would bill per token, so it is removed before the '
          + 'Claude Code CLI is called. Unset it in the shell you start the server '
          + 'from to be certain.'
        );
      }
      if (kept.length) {
        lines.push(
          kept.map(function (n) { return n.var; }).join(', ')
          + ' is set and is passed through, since it only redirects the endpoint.'
        );
      }
      banner(
        $('env-warning'),
        'Environment note: check how this run is billed.',
        lines.join('\n\n')
      );
    }

    const dropzone = $('dropzone');
    const fileInput = $('file-input');
    dropzone.addEventListener('click', function () { fileInput.click(); });
    fileInput.addEventListener('change', function () {
      addFiles(fileInput.files);
      fileInput.value = '';
    });
    ['dragenter', 'dragover'].forEach(function (type) {
      dropzone.addEventListener(type, function (event) {
        event.preventDefault();
        dropzone.classList.add('hot');
      });
    });
    ['dragleave', 'dragend'].forEach(function (type) {
      dropzone.addEventListener(type, function () { dropzone.classList.remove('hot'); });
    });
    dropzone.addEventListener('drop', function (event) {
      event.preventDefault();
      dropzone.classList.remove('hot');
      if (event.dataTransfer && event.dataTransfer.files) addFiles(event.dataTransfer.files);
    });
    // Dropping anywhere else should not navigate away from the app.
    window.addEventListener('dragover', function (event) { event.preventDefault(); });
    window.addEventListener('drop', function (event) { event.preventDefault(); });

    $('convert').addEventListener('click', convert);
    $('clear').addEventListener('click', clearPages);
    $('regenerate').addEventListener('click', convert);
    $('save').addEventListener('click', saveNote);
    $('copy').addEventListener('click', async function () {
      try {
        await navigator.clipboard.writeText($('out-markdown').value);
        $('copy').textContent = 'Copied';
        setTimeout(function () { $('copy').textContent = 'Copy markdown'; }, 1400);
      } catch (error) {
        $('out-markdown').select();
      }
    });

    let renderTimer = null;
    $('out-markdown').addEventListener('input', function () {
      clearTimeout(renderTimer);
      renderTimer = setTimeout(renderPreview, 220);
    });
    ['out-title', 'out-subject', 'out-date'].forEach(function (id) {
      $(id).addEventListener('change', applyMetaFields);
    });

    $('tab-convert').addEventListener('click', function () { switchTab('convert'); });
    $('tab-library').addEventListener('click', function () { switchTab('library'); });
    $('back').addEventListener('click', function () {
      show($('library-detail'), false);
      show($('library-list'), true);
      state.openNote = null;
    });
    $('note-edit').addEventListener('click', function () { setEditing(true); });
    $('note-cancel').addEventListener('click', function () {
      if (state.openNote) $('note-editor').value = state.openNote.markdown;
      setEditing(false);
    });
    $('note-save').addEventListener('click', saveOpenNote);
    $('note-delete').addEventListener('click', deleteOpenNote);

    loadNotes();
  }

  init();
})();
