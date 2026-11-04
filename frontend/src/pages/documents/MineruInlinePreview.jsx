import { useLayoutEffect, useRef } from 'react';
import { api } from '../../api.js';
import { removeBlankLines } from './parseResultSave.js';

function normalizePath(value) {
  try {
    return decodeURIComponent(String(value || ''))
      .replaceAll('\\', '/')
      .replace(/^\.\//, '')
      .replace(/^\//, '')
      .split(/[?#]/, 1)[0];
  } catch {
    return String(value || '').replaceAll('\\', '/').replace(/^\.\//, '').replace(/^\//, '');
  }
}

function imageLookupKeys(value) {
  const normalized = normalizePath(value);
  if (!normalized) return [];
  const keys = [normalized];
  const marker = normalized.indexOf('images/');
  if (marker > 0) keys.push(normalized.slice(marker));
  return [...new Set(keys)];
}

function buildImageMap(images) {
  const map = new Map();
  const basenameOwners = new Map();
  for (const image of Array.isArray(images) ? images : []) {
    if (!image) continue;
    const url = image.url || image.image_url;
    if (!url) continue;
    const value = { ...image, url: api.perceptionAssetUrl(url) };
    for (const source of [image.url, image.image_url, image.relativePath, image.relative_path, image.path, image.source_locator]) {
      for (const key of imageLookupKeys(source)) {
        map.set(key, value);
        const name = key.split('/').pop();
        if (!name) continue;
        const owner = basenameOwners.get(name);
        if (!owner) {
          basenameOwners.set(name, value);
          map.set(name, value);
        } else if (owner !== value) {
          basenameOwners.set(name, null);
          map.delete(name);
        }
      }
    }
  }
  return map;
}

function sourceForImage(source, imageMap) {
  for (const key of imageLookupKeys(source)) {
    const image = imageMap.get(key);
    if (image) return image;
  }
  return undefined;
}

function positivePage(value) {
  const page = Number(value);
  return Number.isInteger(page) && page > 0 ? page : null;
}

function imageSource(image) {
  return image?.relativePath || image?.relative_path || image?.path || image?.url || image?.image_url || '';
}

function imageCanonicalIdentity(image, fallback = '') {
  return String(
    image?.contentHash
    || image?.content_hash
    || image?.minio_object_key
    || image?.image_id
    || fallback
    || '',
  ).trim();
}

function uniqueImagePaths(paths, images = []) {
  const identities = new Map();
  for (const image of Array.isArray(images) ? images : []) {
    const identity = imageCanonicalIdentity(image);
    if (!identity) continue;
    for (const source of [image.url, image.image_url, image.relativePath, image.relative_path, image.path]) {
      for (const key of imageLookupKeys(source)) identities.set(key, identity);
    }
  }
  const seen = new Set();
  return (paths || []).filter((path) => {
    const key = imageLookupKeys(path).find((item) => item.includes('images/')) || normalizePath(path);
    const imageIdentity = identities.get(key) || key;
    if (!imageIdentity || seen.has(imageIdentity)) return false;
    seen.add(imageIdentity);
    return true;
  });
}

function imagePathsByPage(images, blocks) {
  const blockPages = new Map();
  for (const block of Array.isArray(blocks) ? blocks : []) {
    const page = positivePage(
      block?.page_number
      ?? block?.metadata?.page_start
      ?? block?.metadata?.slide_start
      ?? block?.metadata?.page_number,
    );
    if (page == null || !block?.block_id) continue;
    blockPages.set(String(block.block_id), page);
  }
  const paths = new Map();
  for (const image of Array.isArray(images) ? images : []) {
    const source = imageSource(image);
    if (!source) continue;
    const pages = new Set();
    const directPage = positivePage(image?.pageNumber ?? image?.page_number ?? image?.page);
    if (directPage != null) pages.add(directPage);
    for (const blockId of image?.nearby_block_ids || []) {
      const page = blockPages.get(String(blockId));
      if (page != null) pages.add(page);
    }
    for (const page of pages) {
      const pagePaths = paths.get(page) || [];
      paths.set(page, uniqueImagePaths([...pagePaths, source], images));
    }
  }
  return paths;
}

function displayMath(value) {
  return value.replace(/\$([^$]+)\$/g, (_, expression) => expression
    .replace(/\\boldsymbol\s*\{([^{}]*)\}/g, '$1')
    .replace(/\\mathrm\s*\{([^{}]*)\}/g, '$1')
    .replace(/\\cdot/g, '·')
    .replace(/\\times/g, '×')
    .replace(/\\le/g, '≤')
    .replace(/\\ge/g, '≥')
    .replace(/\\pm/g, '±')
    .replace(/\s+/g, ' ')
    .trim());
}

function tableRows(markup) {
  if (typeof DOMParser === 'undefined') return [];
  const document = new DOMParser().parseFromString(markup, 'text/html');
  return Array.from(document.querySelectorAll('tr')).map((row) => Array.from(row.children)
    .filter((cell) => ['TD', 'TH'].includes(cell.tagName))
    .map((cell) => ({
      text: cell.textContent?.trim() || '',
      header: cell.tagName === 'TH',
      rowSpan: Number(cell.getAttribute('rowspan')) || 1,
      colSpan: Number(cell.getAttribute('colspan')) || 1,
    })));
}

function findPageText(source, value, cursor) {
  const text = String(value || '').trim();
  if (!text) return -1;
  const exact = source.indexOf(text, cursor);
  if (exact >= 0) return exact;
  const compact = text.replace(/\s+/g, ' ');
  if (compact.length < 12) return -1;
  const escaped = compact.split(' ').map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('\\s+');
  const match = source.slice(cursor).match(new RegExp(escaped, 'i'));
  if (match) return cursor + match.index;
  const fileName = normalizePath(text).split('/').pop();
  if (!fileName || fileName === text) return -1;
  const escapedFileName = fileName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const fileMatch = source.slice(cursor).match(new RegExp(escapedFileName, 'i'));
  return fileMatch ? cursor + fileMatch.index : -1;
}

function imageIdentity(value, images = []) {
  const normalized = normalizePath(value);
  if (!normalized) return '';
  const lookupKeys = imageLookupKeys(value);
  const candidates = (Array.isArray(images) ? images : []).filter((image) => {
    const aliases = [image?.url, image?.image_url, image?.relativePath, image?.relative_path, image?.path]
      .flatMap((item) => imageLookupKeys(item));
    return aliases.some((alias) => lookupKeys.includes(alias));
  });
  const identities = [...new Set(candidates
    .map((image) => imageCanonicalIdentity(image, normalized))
    .filter(Boolean))];
  if (identities.length === 1) return identities[0];
  if (candidates.length === 1) return imageCanonicalIdentity(candidates[0], normalized);
  return normalized;
}

function sourceContainsImage(source, value, images = []) {
  const normalized = normalizePath(value);
  const basename = normalized.split('/').pop()?.toLowerCase() || '';
  const extensionless = basename.replace(/\.[^.]+$/, '');
  const sourceText = String(source || '').toLowerCase();
  const targetIdentity = imageIdentity(value, images);
  const basenameOwners = new Set((Array.isArray(images) ? images : [])
    .filter((image) => [image?.url, image?.image_url, image?.relativePath, image?.relative_path, image?.path]
      .some((alias) => normalizePath(alias).split('/').pop()?.toLowerCase() === basename))
    .map((image) => imageIdentity(imageSource(image), images)));
  if (Array.isArray(images) && images.some((image) => {
    if (imageIdentity(imageSource(image), images) !== targetIdentity) return false;
    return [image?.url, image?.image_url, image?.relativePath, image?.relative_path, image?.path]
      .some((alias) => {
        const normalizedAlias = normalizePath(alias);
        return normalizedAlias && sourceText.includes(normalizedAlias.toLowerCase());
      });
  })) return true;
  return normalized && (
    source.includes(value)
    || source.includes(normalized)
    || (basenameOwners.size <= 1 && sourceText.includes(basename))
    // 受控图片地址通常没有扩展名，但 MinerU 清单路径带 .jpg/.png。
    || (basenameOwners.size <= 1 && extensionless.length >= 8 && sourceText.includes(extensionless))
  );
}

function pageBoundaryPosition(source, position) {
  const lineStart = source.lastIndexOf('\n', position - 1) + 1;
  const previousEnd = lineStart - 1;
  const previousStart = previousEnd >= 0 ? source.lastIndexOf('\n', previousEnd - 1) + 1 : -1;
  const previousLine = previousStart >= 0 ? source.slice(previousStart, lineStart).trim() : '';
  return /^#{1,6}$/.test(previousLine) ? previousStart : lineStart;
}

function buildPageAwareText(text, blocks = [], pageTexts = [], pageManifest = [], images = []) {
  const source = String(text || '');
  const candidates = new Map();
  const imagePaths = imagePathsByPage(images, blocks);
  for (const [page, value] of Object.entries(pageTexts || {})) {
    candidates.set(Number(page), [String(value || '')]);
  }
  for (const block of Array.isArray(blocks) ? blocks : []) {
    const page = block?.page_number ?? block?.metadata?.page_start ?? block?.metadata?.page_number ?? block?.metadata?.pageNumber;
    if (page == null) continue;
    candidates.set(Number(page), [...(candidates.get(Number(page)) || []), block.text]);
  }
  for (const image of Array.isArray(images) ? images : []) {
    const page = image?.pageNumber ?? image?.page_number;
    const sourcePath = imageSource(image) || image?.source_locator;
    if (page != null && sourcePath) candidates.set(Number(page), [...(candidates.get(Number(page)) || []), sourcePath]);
  }
  const pages = (Array.isArray(pageManifest) ? pageManifest : [])
    .map((page) => ({
      number: Number(page?.page_number ?? page?.pageNumber),
      text: String(page?.text || ''),
      imagePaths: uniqueImagePaths([
        ...(Array.isArray(page?.image_paths) ? page.image_paths : []),
        ...(imagePaths.get(Number(page?.page_number ?? page?.pageNumber)) || []),
      ], images),
    }))
    .filter((page) => Number.isFinite(page.number) && page.number > 0)
    .sort((left, right) => left.number - right.number);
  if (!pages.length) return addPageMarkers(source, candidates, imagePaths, images);

  const matchedPositions = new Map();
  let searchCursor = 0;
  for (const page of pages) {
    const values = [...page.imagePaths, ...(candidates.get(page.number) || []), page.text]
      .filter(Boolean);
    const positions = values
      .map((value) => findPageText(source, value, searchCursor))
      .filter((value) => value >= 0);
    const position = positions.length ? Math.min(...positions) : -1;
    if (position >= 0) {
      matchedPositions.set(page.number, pageBoundaryPosition(source, position));
      searchCursor = Math.max(searchCursor, position + 1);
    }
  }
  let nextPosition = source.length;
  const positions = new Map();
  for (let index = pages.length - 1; index >= 0; index -= 1) {
    const page = pages[index];
    if (matchedPositions.has(page.number)) nextPosition = matchedPositions.get(page.number);
    positions.set(page.number, nextPosition);
  }
  const markers = pages.map((page) => ({
    position: positions.get(page.number),
    page: page.number,
    imagePaths: page.imagePaths.filter((path) => !sourceContainsImage(source, path, images)),
    missing: !page.text.trim() && page.imagePaths.length === 0,
  }));
  return renderPageMarkers(source, markers, images);
}

function addPageMarkers(source, candidates, imagePaths = new Map(), images = []) {
  const markers = [];
  let cursor = 0;
  for (const page of [...candidates.keys()].filter(Number.isFinite).sort((left, right) => left - right)) {
    const position = candidates.get(page).map((value) => findPageText(source, value, cursor)).find((value) => value >= 0);
    if (position == null) continue;
    markers.push({
      position: pageBoundaryPosition(source, position),
      page,
      imagePaths: imagePaths.get(page) || [],
    });
    cursor = Math.max(cursor, position + 1);
  }
  const uniqueMarkers = markers.filter((marker, index) => index === 0 || marker.page !== markers[index - 1].page);
  return renderPageMarkers(source, uniqueMarkers, images);
}

function renderPageMarkers(source, markers, images = []) {
  if (!markers.length) return source;
  let result = '';
  let offset = 0;
  const renderedImages = new Set();
  for (const marker of markers) {
    result += source.slice(offset, marker.position);
    result += `\n\n[[PAGE_BREAK:${marker.page}]]\n\n`;
    for (const imagePath of marker.imagePaths || []) {
      const identity = imageIdentity(imagePath, images);
      if (!identity || renderedImages.has(identity)) continue;
      renderedImages.add(identity);
      result += `![${imagePath}](${imagePath})\n`;
    }
    if (marker.missing) result += `[[PAGE_EMPTY:${marker.page}]]\n`;
    offset = marker.position;
  }
  return result + source.slice(offset);
}

function tokenParts(text) {
  const pattern = /(\[\[PAGE_BREAK:(\d+)\]\])|(\[\[PAGE_EMPTY:(\d+)\]\])|(<table\b[\s\S]*?<\/table>)|!\[([\s\S]*?)\]\(((?:[^()\s]|\([^()\s]*\))+)(?:\s+["'][^)]*["'])?\)|<img\b[^>]*\bsrc=["']([^"']+)["'][^>]*>|((?:^|\s)(?:https?:\/\/|\.?\/?(?:auto\/)?images\/)[^\s<>)"';，。；]+\.(?:png|jpe?g|gif|webp|svg)(?:\?[^\s<>)"';，。；]+)?)/gi;
  const parts = [];
  let cursor = 0;
  let match;
  while ((match = pattern.exec(text))) {
    if (match.index > cursor) parts.push({ type: 'text', value: text.slice(cursor, match.index) });
    if (match[1]) {
      parts.push({ type: 'page', value: match[2] });
    } else if (match[3]) {
      parts.push({ type: 'empty-page', value: match[4] });
    } else if (match[5]) {
      parts.push({ type: 'table', value: match[5] });
    } else {
      const source = match[7] || match[8] || String(match[9] || '').trim();
      const prefix = match[9] && String(match[9]).startsWith(' ') ? ' ' : '';
      parts.push({ type: 'image', alt: match[6] || '', source, prefix });
    }
    cursor = pattern.lastIndex;
  }
  if (cursor < text.length) parts.push({ type: 'text', value: text.slice(cursor) });
  return parts.length ? parts : [{ type: 'text', value: text }];
}

function escapeHtml(value) {
  return String(value || '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[character]));
}

function renderPreviewHtml(parts, imageMap, editable) {
  return parts.map((part) => {
    if (part.type === 'page') return `<div class="mineru-page-break" data-page-break="true" contenteditable="false"><span>第 ${escapeHtml(part.value)} 页</span></div>`;
    if (part.type === 'empty-page') return `<div class="mineru-empty-page" data-page-empty="true" contenteditable="false">第 ${escapeHtml(part.value)} 页未提取到文字，可能是图片页或空白页</div>`;
    if (part.type === 'text') {
      const text = editable ? part.value.replace(/\n{2,}/g, '\n') : part.value;
      return `<span style="font-size:17px;line-height:1">${escapeHtml(displayMath(text))}</span>`;
    }
    if (part.type === 'table') {
      const rows = tableRows(part.value).map((row) => `<tr>${row.map((cell) => {
        const tag = cell.header ? 'th' : 'td';
        return `<${tag} rowspan="${cell.rowSpan}" colspan="${cell.colSpan}">${escapeHtml(cell.text)}</${tag}>`;
      }).join('')}</tr>`).join('');
      return `<div class="mineru-inline-table" data-table-source="${escapeHtml(part.value)}" contenteditable="${editable}"><table><tbody>${rows}</tbody></table></div>${editable ? '<span class="mineru-image-caret" data-image-caret="true" contenteditable="true"></span>' : ''}`;
    }
    const image = sourceForImage(part.source, imageMap);
    if (!image) return `<span class="mineru-image-unresolved" data-image-source="${escapeHtml(part.source)}" data-image-alt="${escapeHtml(part.alt)}" contenteditable="${editable}">${escapeHtml(part.prefix)}［图片未关联：${escapeHtml(part.source)}］</span>${editable ? '<span class="mineru-image-caret" data-image-caret="true" contenteditable="true"></span>' : ''}`;
    return `<figure class="mineru-inline-image" data-image-source="${escapeHtml(part.source)}" data-image-alt="${escapeHtml(part.alt)}" contenteditable="${editable}"><img src="${escapeHtml(image.url)}" alt="${escapeHtml(part.alt || image.caption || 'MinerU 解析图片')}" loading="lazy" /></figure>${editable ? '<span class="mineru-image-caret" data-image-caret="true" contenteditable="true"></span>' : ''}`;
  }).join('');
}

function sanitizePreviewHtml(value) {
  if (typeof DOMParser === 'undefined') return '';
  const document = new DOMParser().parseFromString(String(value || ''), 'text/html');
  const allowedTags = new Set(['DIV', 'SPAN', 'FIGURE', 'IMG', 'TABLE', 'TBODY', 'TR', 'TD', 'TH']);
  const allowedAttributes = new Set([
    'alt', 'class', 'colspan', 'contenteditable', 'data-image-alt', 'data-image-source',
    'data-page-break', 'data-page-empty', 'data-table-source', 'loading', 'rowspan', 'src', 'style',
  ]);
  document.body.querySelectorAll('*').forEach((element) => {
    if (!allowedTags.has(element.tagName)) {
      element.replaceWith(document.createTextNode(element.textContent || ''));
      return;
    }
    Array.from(element.attributes).forEach((attribute) => {
      const name = attribute.name.toLowerCase();
      if (!allowedAttributes.has(name) || name.startsWith('on')) element.removeAttribute(attribute.name);
    });
    if (element.tagName === 'IMG' && !/^(?:https?:|\/|data:image\/)/i.test(element.getAttribute('src') || '')) {
      element.remove();
    }
  });
  return document.body.innerHTML;
}

function serializePreview(root) {
  return Array.from(root.childNodes).map((node) => {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    if (node.dataset.imageCaret) return '';
    if (node.dataset.pageBreak) return '';
    if (node.dataset.pageEmpty) return '';
    if (node.dataset.tableSource) return node.querySelector('table')?.outerHTML || node.dataset.tableSource;
    if (node.dataset.imageSource) {
      // 删除图片时浏览器可能只移除 figure 内的 img，不能把空 figure 再序列化回图片标记。
      if (node.tagName === 'FIGURE' && !node.querySelector('img')) return '';
      return `![${node.dataset.imageAlt || ''}](${node.dataset.imageSource})`;
    }
    if (node.tagName === 'BR') return '\n';
    const value = serializePreview(node);
    return ['DIV', 'P'].includes(node.tagName) ? `${value}\n` : value;
  }).join('');
}

function deduplicateSerializedImages(text, images = []) {
  const seen = new Set();
  const pattern = /!\[([\s\S]*?)\]\(((?:[^()\s]|\([^()\s]*\))+)(?:\s+["'][^)]*["'])?\)/g;
  return String(text || '').replace(pattern, (match, _alt, source) => {
    const identity = imageIdentity(source, images) || normalizePath(source);
    if (!identity || seen.has(identity)) return '';
    seen.add(identity);
    return match;
  });
}

function clearIfOnlyNonTextContent(root) {
  root.querySelectorAll('[data-image-source]').forEach((node) => {
    if (node.tagName === 'FIGURE' && !node.querySelector('img')) node.remove();
  });
  const copy = root.cloneNode(true);
  copy.querySelectorAll('[data-image-source], [data-page-break], [data-page-empty], [data-image-caret]').forEach((node) => node.remove());
  if (!copy.textContent?.trim()) root.innerHTML = '';
}

/** Render MinerU原文，并把原文中的图片标记替换为受控图片。 */
export function MineruInlinePreview({ text, images = [], blocks = [], pageTexts = {}, pageManifest = [], editable = false, onChange, onDraftChange }) {
  const editedRef = useRef(false);
  const pendingTextRef = useRef(null);
  const previewRef = useRef(null);
  const renderedTextRef = useRef(null);
  const renderedHtmlRef = useRef('');
  const scrollRef = useRef({ top: 0, left: 0 });
  const pageScrollRef = useRef({ top: 0, left: 0 });
  const beforeInputCapturedRef = useRef(false);
  const imageMap = buildImageMap(images);
  const propText = String(text || '');
  const pendingText = pendingTextRef.current;
  const usePendingText = editable && (
    editedRef.current || (pendingText !== null && pendingText === propText)
  );
  const rawSourceText = usePendingText
    ? String(pendingText ?? propText)
    : buildPageAwareText(text, blocks, pageTexts, pageManifest, images);
  const sourceText = deduplicateSerializedImages(editable ? removeBlankLines(rawSourceText) : rawSourceText, images);
  const parts = tokenParts(sourceText);
  const html = sanitizePreviewHtml(renderPreviewHtml(parts, imageMap, editable));
  const syncEditedText = (root) => {
    editedRef.current = true;
    clearIfOnlyNonTextContent(root);
    const serialized = deduplicateSerializedImages(serializePreview(root), images);
    onDraftChange?.(editable ? removeBlankLines(serialized) : serialized);
  };
  const captureScrollPosition = () => {
    const preview = previewRef.current;
    if (preview) scrollRef.current = { top: preview.scrollTop, left: preview.scrollLeft };
    pageScrollRef.current = { top: window.scrollY, left: window.scrollX };
  };
  const restoreScrollPosition = () => {
    const preview = previewRef.current;
    if (preview) {
      preview.scrollTop = scrollRef.current.top;
      preview.scrollLeft = scrollRef.current.left;
    }
    window.scrollTo(pageScrollRef.current.left, pageScrollRef.current.top);
  };
  const restoreAfterInput = () => {
    restoreScrollPosition();
    requestAnimationFrame(restoreScrollPosition);
  };
  useLayoutEffect(() => {
    const node = previewRef.current;
    if (
      editable
      && editedRef.current
      && renderedTextRef.current !== null
      && String(text || '') !== String(renderedTextRef.current || '')
    ) {
      editedRef.current = false;
      pendingTextRef.current = null;
    }
    if (!editedRef.current && pendingTextRef.current !== null && String(text || '') !== pendingTextRef.current) {
      pendingTextRef.current = null;
    } else if (pendingTextRef.current !== null && String(text || '') === pendingTextRef.current) {
      pendingTextRef.current = null;
    }
    if (!node || (editable && editedRef.current)) return;
    if (renderedTextRef.current === text && renderedHtmlRef.current === html && node.childNodes.length) return;
    node.innerHTML = html;
    renderedTextRef.current = text;
    renderedHtmlRef.current = html;
    restoreScrollPosition();
  }, [editable, html, text]);
  return (
    <div
      ref={previewRef}
      className={`mineru-inline-preview${editable ? ' is-editable' : ''}`}
      style={editable ? { fontSize: '17px', lineHeight: 1 } : undefined}
      contentEditable={editable}
      suppressContentEditableWarning
      onScroll={editable ? (event) => {
        scrollRef.current = { top: event.currentTarget.scrollTop, left: event.currentTarget.scrollLeft };
      } : undefined}
      onBeforeInput={editable ? () => {
        captureScrollPosition();
        beforeInputCapturedRef.current = true;
      } : undefined}
      onClick={editable ? (event) => {
        const image = event.target.closest?.('[data-image-source]');
        if (!image || !previewRef.current?.contains(image)) return;
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNode(image);
        selection?.removeAllRanges();
        selection?.addRange(range);
      } : undefined}
      onKeyDown={editable ? (event) => {
        if (!['Backspace', 'Delete'].includes(event.key)) return;
        const selection = window.getSelection();
        if (!selection?.rangeCount || selection.isCollapsed) return;
        const range = selection.getRangeAt(0);
        if (!previewRef.current?.contains(range.commonAncestorContainer)) return;
        const selected = range.cloneContents();
        if (!selected.querySelector?.('[data-image-source]')) return;
        event.preventDefault();
        range.deleteContents();
        syncEditedText(event.currentTarget);
      } : undefined}
      onInput={editable ? (event) => {
        if (!beforeInputCapturedRef.current) captureScrollPosition();
        beforeInputCapturedRef.current = false;
        syncEditedText(event.currentTarget);
        restoreAfterInput();
      } : undefined}
      onBlur={editable && onChange ? (event) => {
      captureScrollPosition();
      if (!editedRef.current) return;
        const nextText = deduplicateSerializedImages(serializePreview(event.currentTarget), images);
        pendingTextRef.current = nextText;
        onChange(nextText);
        editedRef.current = false;
        restoreAfterInput();
      } : undefined}
    />
  );
}
