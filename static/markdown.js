/* A small markdown renderer, just enough for the notes this app produces.
   Deliberately dependency-free so the app works with no network and no build
   step. It handles: front matter, headings, paragraphs, bullet and numbered
   lists (two levels), blockquotes, fenced and inline code, tables, rules,
   images, links, bold/italic, and the app's own [unclear: ...] markers. */

(function (global) {
  'use strict';

  function escapeHtml(text) {
    return text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function splitFrontMatter(text) {
    if (!text.startsWith('---')) return { meta: null, body: text };
    const end = text.indexOf('\n---', 3);
    if (end === -1) return { meta: null, body: text };
    const block = text.slice(3, end).replace(/^\n/, '');
    const rest = text.slice(end + 4).replace(/^\n/, '');
    const meta = {};
    block.split('\n').forEach(function (line) {
      const idx = line.indexOf(':');
      if (idx <= 0) return;
      const key = line.slice(0, idx).trim();
      let value = line.slice(idx + 1).trim();
      if (value.startsWith('[') && value.endsWith(']')) {
        meta[key] = value
          .slice(1, -1)
          .split(',')
          .map(function (item) { return item.trim().replace(/^["']|["']$/g, ''); })
          .filter(Boolean);
      } else {
        meta[key] = value.replace(/^["']|["']$/g, '');
      }
    });
    return { meta: meta, body: rest };
  }

  // Math spans are pulled out before markdown runs and put back afterwards.
  // Without this, `\begin{bmatrix}a & b \\ c & d\end{bmatrix}` loses its `\\`
  // row breaks and `x_1 * y_1` turns into emphasis. Collected per render pass.
  let mathSpans = [];

  // Apply the math scan everywhere except inside fenced code, where a dollar
  // sign is just a dollar sign.
  function stashMathOutsideFences(text) {
    const lines = text.split('\n');
    const out = [];
    let buffer = [];
    let inFence = false;

    function flush() {
      if (buffer.length) {
        out.push(stashMath(buffer.join('\n')));
        buffer = [];
      }
    }

    lines.forEach(function (line) {
      if (/^\s*```/.test(line)) {
        if (!inFence) flush();
        inFence = !inFence;
        out.push(line);
        return;
      }
      if (inFence) out.push(line);
      else buffer.push(line);
    });
    flush();
    return out.join('\n');
  }

  function stashMath(text) {
    // $$...$$ and \[...\] are display; $...$ and \(...\) are inline. A bare
    // dollar amount ("$5 and $10") must not start a math span, so inline $...$
    // requires a non-space character just inside each delimiter.
    return text
      .replace(/\$\$([\s\S]+?)\$\$/g, function (_m, tex) { return keepMath(tex, true); })
      .replace(/\\\[([\s\S]+?)\\\]/g, function (_m, tex) { return keepMath(tex, true); })
      .replace(/\\\(([\s\S]+?)\\\)/g, function (_m, tex) { return keepMath(tex, false); })
      .replace(/\$(?!\s)((?:[^$\\\n]|\\.)+?)(?<!\s)\$/g, function (_m, tex) {
        return keepMath(tex, false);
      });
  }

  function keepMath(tex, display) {
    mathSpans.push({ tex: tex, display: display });
    return '\u0000MATH' + (mathSpans.length - 1) + '\u0000';
  }

  function restoreMath(html) {
    return html.replace(/\u0000MATH(\d+)\u0000/g, function (_m, index) {
      const span = mathSpans[Number(index)];
      if (!span) return '';
      // KaTeX renders these in place after the HTML lands in the document;
      // if it is unavailable the raw TeX still shows, which beats nothing.
      const tag = span.display ? 'div' : 'span';
      const cls = span.display ? 'math math-display' : 'math math-inline';
      return '<' + tag + ' class="' + cls + '">' + escapeHtml(span.tex) + '</' + tag + '>';
    });
  }

  function renderInline(text, resolveImage) {
    const codes = [];
    let out = escapeHtml(text);

    // Protect inline code from every other rule.
    out = out.replace(/`([^`]+)`/g, function (_m, code) {
      codes.push(code);
      return '\u0000CODE' + (codes.length - 1) + '\u0000';
    });

    out = out.replace(/\[unclear:\s*([^\]]*)\]/gi, function (_m, guess) {
      const inner = guess.trim();
      return (
        '<span class="unclear" title="Claude was not confident of this transcription">' +
        (inner || '&hellip;') +
        '</span>'
      );
    });

    out = out.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, function (_m, alt, src) {
      const url = resolveImage ? resolveImage(src) : src;
      return '<img src="' + url + '" alt="' + alt + '" loading="lazy">';
    });

    out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (_m, label, href) {
      const safe = /^(https?:|mailto:|\/)/i.test(href) ? href : '#';
      return '<a href="' + safe + '" target="_blank" rel="noreferrer">' + label + '</a>';
    });

    out = out
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/__([^_]+)__/g, '<strong>$1</strong>')
      .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>')
      .replace(/(^|[\s(])_([^_\n]+)_(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>');

    out = out.replace(/\u0000CODE(\d+)\u0000/g, function (_m, index) {
      return '<code>' + codes[Number(index)] + '</code>';
    });

    return out;
  }

  function renderTable(rows, resolveImage) {
    const cells = rows.map(function (row) {
      return row
        .trim()
        .replace(/^\||\|$/g, '')
        .split('|')
        .map(function (cell) { return cell.trim(); });
    });
    const head = cells.shift();
    if (cells.length && /^[:\-\s|]+$/.test(rows[1] || '')) cells.shift();
    let html = '<table><thead><tr>';
    head.forEach(function (cell) {
      html += '<th>' + renderInline(cell, resolveImage) + '</th>';
    });
    html += '</tr></thead><tbody>';
    cells.forEach(function (row) {
      html += '<tr>';
      row.forEach(function (cell) {
        html += '<td>' + renderInline(cell, resolveImage) + '</td>';
      });
      html += '</tr>';
    });
    return html + '</tbody></table>';
  }

  const BULLET = /^(\s*)[-*+]\s+(.*)$/;
  const ORDERED = /^(\s*)(\d+)[.)]\s+(.*)$/;

  function renderList(lines, resolveImage) {
    // lines: [{ indent, text, ordered }]
    let html = '';
    const open = [];

    function openList(ordered) {
      open.push(ordered);
      html += ordered ? '<ol>' : '<ul>';
    }
    function closeList() {
      html += open.pop() ? '</ol>' : '</ul>';
    }

    let currentDepth = -1;
    lines.forEach(function (item) {
      const depth = item.indent >= 2 ? 1 : 0;
      if (depth > currentDepth) {
        openList(item.ordered);
      } else if (depth < currentDepth) {
        while (open.length - 1 > depth) closeList();
      }
      currentDepth = depth;
      html += '<li>' + renderInline(item.text, resolveImage) + '</li>';
    });
    while (open.length) closeList();
    return html;
  }

  function render(markdown, options) {
    options = options || {};
    const resolveImage = options.resolveImage;
    const split = splitFrontMatter(markdown || '');

    // Only the outermost call stashes and restores: nested calls (blockquotes)
    // receive text whose math is already tokenised, and share the same table.
    const outermost = !options._nested;
    if (outermost) mathSpans = [];
    const body = outermost ? stashMathOutsideFences(split.body) : split.body;
    const lines = body.split('\n');
    let html = '';
    let index = 0;

    while (index < lines.length) {
      const line = lines[index];

      if (!line.trim()) { index++; continue; }

      // Fenced code
      const fence = line.match(/^\s*```(\w*)\s*$/);
      if (fence) {
        const buffer = [];
        index++;
        while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
          buffer.push(lines[index]);
          index++;
        }
        index++;
        html += '<pre><code>' + escapeHtml(buffer.join('\n')) + '</code></pre>';
        continue;
      }

      // Heading
      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        const level = heading[1].length;
        html += '<h' + level + '>' + renderInline(heading[2], resolveImage) + '</h' + level + '>';
        index++;
        continue;
      }

      // Horizontal rule
      if (/^\s*([-*_])\s*(\1\s*){2,}$/.test(line)) {
        html += '<hr>';
        index++;
        continue;
      }

      // Blockquote
      if (/^\s*>/.test(line)) {
        const buffer = [];
        while (index < lines.length && /^\s*>/.test(lines[index])) {
          buffer.push(lines[index].replace(/^\s*>\s?/, ''));
          index++;
        }
        html += '<blockquote>'
          + render(buffer.join('\n'), Object.assign({}, options, { _nested: true })).html
          + '</blockquote>';
        continue;
      }

      // Table
      if (/^\s*\|.*\|\s*$/.test(line)) {
        const buffer = [];
        while (index < lines.length && /^\s*\|.*\|\s*$/.test(lines[index])) {
          buffer.push(lines[index]);
          index++;
        }
        html += renderTable(buffer, resolveImage);
        continue;
      }

      // Lists
      if (BULLET.test(line) || ORDERED.test(line)) {
        const items = [];
        while (index < lines.length && (BULLET.test(lines[index]) || ORDERED.test(lines[index]))) {
          const bullet = lines[index].match(BULLET);
          if (bullet) {
            items.push({ indent: bullet[1].length, text: bullet[2], ordered: false });
          } else {
            const ordered = lines[index].match(ORDERED);
            items.push({ indent: ordered[1].length, text: ordered[3], ordered: true });
          }
          index++;
        }
        html += renderList(items, resolveImage);
        continue;
      }

      // Paragraph
      const paragraph = [];
      while (
        index < lines.length &&
        lines[index].trim() &&
        !/^(#{1,6})\s/.test(lines[index]) &&
        !/^\s*```/.test(lines[index]) &&
        !/^\s*>/.test(lines[index]) &&
        !/^\s*\|.*\|\s*$/.test(lines[index]) &&
        !BULLET.test(lines[index]) &&
        !ORDERED.test(lines[index])
      ) {
        paragraph.push(lines[index].trim());
        index++;
      }
      const text = renderInline(paragraph.join(' '), resolveImage);
      // A lone image, or a display equation on its own, reads better without a
      // paragraph wrapper - and a <div> inside a <p> is invalid HTML anyway.
      const loneMath = text.match(/^\u0000MATH(\d+)\u0000$/);
      const bare = /^<img[^>]*>$/.test(text)
        || (loneMath && mathSpans[Number(loneMath[1])] && mathSpans[Number(loneMath[1])].display);
      html += bare ? text : '<p>' + text + '</p>';
    }

    if (outermost) html = restoreMath(html);
    return { html: html, meta: split.meta };
  }

  global.NoteMarkdown = { render: render, splitFrontMatter: splitFrontMatter };
})(window);
