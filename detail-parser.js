// Shared detail_text parser for JKK and UR listings.
// JKK format: strict alternating label/value lines (no blank lines between sections).
// UR format:  blank lines separate each section block (header + body lines).
// Used by both dashboard.html and property.html — edit only here.
//
// SKIP: sections that are noise or already displayed elsewhere in the UI.
const _DETAIL_SKIP = new Set([
  '特徴ピックアップ',           // feature list — shown as chips from l.features
  'より詳しい住宅の情報はこちら', // just a link placeholder
]);

function parseDetailSections(text) {
  const sections = [];

  if (text.includes('\n\n')) {
    // UR block format: blank line = section boundary
    for (const block of text.split(/\n\n+/)) {
      const lines = block.split('\n').map(l => l.trim()).filter(Boolean);
      if (!lines.length) continue;
      const header = lines[0];
      if (header === '間取り図') break;
      if (_DETAIL_SKIP.has(header)) continue;
      const unique = [...new Set(lines.slice(1))];
      if (unique.length) sections.push({ header, body: unique.join('\n') });
    }
    return sections;
  }

  // JKK alternating format: label line, value line, label line, value line …
  const isHdr = l => l.length <= 18 && !l.includes('、') && !l.includes('：')
                  && !l.startsWith('・') && !l.startsWith('※') && !l.startsWith('(')
                  && !/^\d/.test(l) && !/^[A-Za-z]\d/.test(l);
  const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line === '間取り図') break;
    if (isHdr(line)) {
      i++;
      const body = [];
      if (i < lines.length) {
        body.push(lines[i++]);
        while (i < lines.length && !isHdr(lines[i])) body.push(lines[i++]);
      }
      const unique = body.filter((l, idx) => body.indexOf(l) === idx);
      if (!_DETAIL_SKIP.has(line) && unique.length)
        sections.push({ header: line, body: unique.join('\n') });
    } else { i++; }
  }
  return sections;
}
