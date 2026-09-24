// Renders the icons and the social-card image into public/ from the logo
// geometry below. Run with `npm run images` after changing the logo or the
// card text; the outputs are committed, so a build never needs this.
//
// Why each file exists:
//   favicon.svg          - modern browsers, crisp at any size, follows dark mode
//   favicon.ico          - Google's search-result favicon needs a raster file at
//                          a multiple of 48px; also old browsers and /favicon.ico probes
//   apple-touch-icon.png - iOS home screen (180px, opaque: iOS fills transparency black)
//   icon-512.png         - the Organization logo in the JSON-LD
//   og.png, og-de.png    - the 1200x630 link-preview card, one per language

import { writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

import sharp from "sharp";

const out = (name) => fileURLToPath(new URL(`../public/${name}`, import.meta.url));

const ACCENT = "#059669";
const ACCENT_DARK = "#34D399";
const BG = "#F6F9F7";
const INK = "#161C19";
const MUTED = "#4A544E";

const LOGO_PATHS = `
  <path d="M54.5 13.3 H86.6 V45.5" stroke-linejoin="miter"/>
  <path d="M45.5 86.7 H13.4 V54.5" stroke-linejoin="miter"/>
  <path d="M29.8 18.6 C55 18.6 71 26 71 37.8 C71 49.6 55 57 30.6 57 L30.6 86.7"/>`;

const logo = (stroke) =>
  `<g fill="none" stroke="${stroke}" stroke-width="9" stroke-linecap="round">${LOGO_PATHS}</g>`;

const faviconSvg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
<style>g{stroke:${ACCENT}} @media (prefers-color-scheme: dark){g{stroke:${ACCENT_DARK}}}</style>
${logo(ACCENT)}
</svg>
`;

/** The logo inset on an opaque tile, for the raster icons. */
const tile = (size, inset) => `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 100 100">
<rect width="100" height="100" fill="#FFFFFF"/>
<g transform="translate(${inset} ${inset}) scale(${(100 - 2 * inset) / 100})">${logo(ACCENT)}</g>
</svg>`;

const OG_TEXT = {
  en: {
    file: "og.png",
    lines: ["A feed carried by people,", "not an algorithm."],
    sub: "No ranking model. Posts travel as far as people pass them on.",
  },
  de: {
    file: "og-de.png",
    lines: ["Ein Feed, getragen von Menschen –", "nicht von einem Algorithmus."],
    sub: "Kein Ranking. Beiträge reisen so weit, wie Menschen sie weitergeben.",
  },
};

const ogSvg = ({ lines, sub }) => `<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
<defs>
  <radialGradient id="glow" cx="78%" cy="40%" r="55%">
    <stop offset="0" stop-color="${ACCENT}" stop-opacity=".16"/>
    <stop offset="1" stop-color="${ACCENT}" stop-opacity="0"/>
  </radialGradient>
</defs>
<rect width="1200" height="630" fill="${BG}"/>
<rect width="1200" height="630" fill="url(#glow)"/>
<g transform="translate(88 92) scale(.78)">${logo(ACCENT)}</g>
<text x="190" y="152" font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="44" font-weight="700" fill="${INK}">Peerkola</text>
<text font-family="Segoe UI, Helvetica, Arial, sans-serif" font-weight="700" fill="${INK}" font-size="${lines.some((l) => l.length > 28) ? 64 : 76}" letter-spacing="-2">
  <tspan x="88" y="330">${lines[0]}</tspan>
  <tspan x="88" y="420">${lines[1]}</tspan>
</text>
<text x="88" y="520" font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="32" fill="${MUTED}">${sub}</text>
<rect x="0" y="618" width="1200" height="12" fill="${ACCENT}"/>
</svg>`;

/** An .ico holding one PNG image - valid since Windows Vista, and what browsers read. */
function pngToIco(png, size) {
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0); // reserved
  header.writeUInt16LE(1, 2); // type: icon
  header.writeUInt16LE(1, 4); // one image
  const entry = Buffer.alloc(16);
  entry.writeUInt8(size >= 256 ? 0 : size, 0);
  entry.writeUInt8(size >= 256 ? 0 : size, 1);
  entry.writeUInt16LE(1, 4); // colour planes
  entry.writeUInt16LE(32, 6); // bits per pixel
  entry.writeUInt32LE(png.length, 8);
  entry.writeUInt32LE(header.length + entry.length, 12);
  return Buffer.concat([header, entry, png]);
}

const render = (svg) => sharp(Buffer.from(svg)).png({ compressionLevel: 9 }).toBuffer();

await writeFile(out("favicon.svg"), faviconSvg);
await writeFile(out("favicon.ico"), pngToIco(await render(tile(48, 8)), 48));
await writeFile(out("apple-touch-icon.png"), await render(tile(180, 16)));
await writeFile(out("icon-512.png"), await render(tile(512, 12)));
for (const card of Object.values(OG_TEXT)) {
  await writeFile(out(card.file), await render(ogSvg(card)));
}

console.log("wrote favicon.svg, favicon.ico, apple-touch-icon.png, icon-512.png, og*.png");
