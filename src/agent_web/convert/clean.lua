-- Pandoc filter for agent-web: keep content and its structure, drop layout wrappers and attributes.
-- Every rule here has a fixture test in tests/test_convert_html.py.

-- Layout wrappers: keep what's inside, drop the wrapper and its classes/ids/styles.
function Div(el) return el.content end
function Span(el) return el.content end

-- Raw HTML that pandoc couldn't map (widgets, embeds): drop it.
function RawBlock(el) return {} end
function RawInline(el) return {} end

-- Links: keep text and target, drop attributes (rel, itemprop, ...) and tooltips. Attributes are
-- what forces gfm to fall back to raw <a> tags.
function Link(el)
  el.attr = pandoc.Attr()
  el.title = ""
  return el
end

-- Images: inline base64 images become their description (⟦⟧ like the other markers, since
-- pandoc would escape [ ]); others lose attributes.
function Image(el)
  if el.src:match("^data:") then
    local alt = pandoc.utils.stringify(el.caption)
    if alt ~= "" then
      return pandoc.Str("⟦image: " .. alt .. "⟧")
    end
    return {}
  end
  el.attr = pandoc.Attr()
  el.title = ""
  return el
end

-- Figures: gfm has no figure syntax (it would emit raw <figure>), so emit the image(s) followed by
-- the caption as a normal paragraph.
function Figure(el)
  local out = pandoc.List(el.content)
  out:extend(el.caption.long)
  return out
end

-- Tables: strip attributes, alignment and column widths. Row and column spans are kept, so a
-- table with merged cells is written as a clean HTML table instead of a misaligned pipe table.
local function clean_rows(rows)
  for _, row in ipairs(rows) do
    row.attr = pandoc.Attr()
    for _, cell in ipairs(row.cells) do
      cell.attr = pandoc.Attr()
      cell.alignment = pandoc.AlignDefault
    end
  end
end

function Table(t)
  for i, _ in ipairs(t.colspecs) do
    t.colspecs[i] = {pandoc.AlignDefault, pandoc.ColWidthDefault}
  end
  t.attr = pandoc.Attr()
  t.head.attr = pandoc.Attr()
  clean_rows(t.head.rows)
  for _, body in ipairs(t.bodies) do
    body.attr = pandoc.Attr()
    clean_rows(body.head)
    clean_rows(body.body)
  end
  t.foot.attr = pandoc.Attr()
  clean_rows(t.foot.rows)
  return t
end

-- Headings and code: ids and classes add noise without content.
function Header(el)
  el.attr = pandoc.Attr()
  return el
end

function CodeBlock(el)
  local lang = el.classes[1]
  el.attr = pandoc.Attr("", lang and {lang} or {})
  return el
end

function Code(el)
  el.attr = pandoc.Attr()
  return el
end
