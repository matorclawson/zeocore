-- zeocore: the allowlist filter for render_email_html (ZEOCORE-SOW-33, 5B).
--
-- It REBUILDS the document from only the allowed nodes. Nothing is copied
-- from the input tree except text, link targets and image sources, so the
-- attributes pandoc would otherwise carry (id, class, key/value pairs), and
-- the nodes its HTML writer would turn into figure, section, span, div or
-- footnotes, can never reach the output.
--
-- A construct outside the subset is a REFUSAL, never a rewrite: the filter
-- raises "ZEOCORE_REFUSE:<code>", and the caller maps the code to a typed,
-- fixed message. Raw HTML written inside the Markdown is not markup: it is
-- turned into plain text, which the writer then escapes.
--
-- The subset is zeonewsroom's (ZEONEWSROOM-SOW-24, design v0 section 4).
-- Image sizes arrive as the document metadata "zeocore_image_sizes", one
-- "src<TAB>width<TAB>height" per line; Markdown has no way to carry them
-- once link_attributes is off.

local function refuse(code)
  error("ZEOCORE_REFUSE:" .. code, 0)
end

local function tag(el)
  return el.tag or el.t
end

local sizes = {}

local function load_sizes(meta)
  local raw = meta["zeocore_image_sizes"]
  if raw == nil then
    return
  end
  local text = pandoc.utils.stringify(raw)
  for line in string.gmatch(text, "[^\n]+") do
    local src, w, h = string.match(line, "^([^\t]+)\t(%d+)\t(%d+)$")
    if src == nil then
      refuse("image_sizes_malformed")
    end
    sizes[src] = { tonumber(w), tonumber(h) }
  end
end

local rebuild_inlines, rebuild_blocks

local function plain_text(inlines)
  return pandoc.utils.stringify(pandoc.Inlines(inlines))
end

local function rebuild_inline(el)
  local t = tag(el)
  if t == "Str" then
    return { pandoc.Str(el.text) }
  elseif t == "Space" then
    return { pandoc.Space() }
  elseif t == "SoftBreak" then
    return { pandoc.SoftBreak() }
  elseif t == "LineBreak" then
    return { pandoc.LineBreak() }
  elseif t == "Emph" then
    return { pandoc.Emph(rebuild_inlines(el.content)) }
  elseif t == "Strong" then
    return { pandoc.Strong(rebuild_inlines(el.content)) }
  elseif t == "Code" then
    return { pandoc.Code(el.text) }
  elseif t == "RawInline" then
    -- Raw markup is data, never markup: keep it as text.
    return { pandoc.Str(el.text) }
  elseif t == "Link" then
    if plain_text(el.content) == "" then
      refuse("link_text_missing")
    end
    return { pandoc.Link(rebuild_inlines(el.content), el.target, el.title) }
  elseif t == "Image" then
    local alt = plain_text(el.caption)
    if alt == "" then
      refuse("image_alt_missing")
    end
    local size = sizes[el.src]
    if size == nil then
      refuse("image_size_missing")
    end
    local attr = pandoc.Attr("", {}, { { "width", tostring(size[1]) }, { "height", tostring(size[2]) } })
    return { pandoc.Image(pandoc.Inlines({ pandoc.Str(alt) }), el.src, el.title, attr) }
  end
  refuse("inline_" .. string.lower(t))
end

rebuild_inlines = function(list)
  local out = {}
  for _, el in ipairs(list) do
    for _, new in ipairs(rebuild_inline(el)) do
      out[#out + 1] = new
    end
  end
  return out
end

local function rebuild_item(blocks)
  return rebuild_blocks(blocks)
end

local function rebuild_block(el)
  local t = tag(el)
  if t == "Para" then
    return { pandoc.Para(rebuild_inlines(el.content)) }
  elseif t == "Plain" then
    return { pandoc.Plain(rebuild_inlines(el.content)) }
  elseif t == "Header" then
    if el.level == 1 then
      refuse("heading_level_1")
    elseif el.level > 3 then
      refuse("heading_level_deep")
    end
    return { pandoc.Header(el.level, rebuild_inlines(el.content)) }
  elseif t == "BulletList" then
    local items = {}
    for _, item in ipairs(el.content) do
      items[#items + 1] = rebuild_item(item)
    end
    return { pandoc.BulletList(items) }
  elseif t == "OrderedList" then
    if el.start ~= 1 then
      refuse("ordered_list_start")
    end
    local items = {}
    for _, item in ipairs(el.content) do
      items[#items + 1] = rebuild_item(item)
    end
    return { pandoc.OrderedList(items) }
  elseif t == "BlockQuote" then
    return { pandoc.BlockQuote(rebuild_blocks(el.content)) }
  elseif t == "HorizontalRule" then
    return { pandoc.HorizontalRule() }
  elseif t == "RawBlock" then
    -- Raw markup is data, never markup: keep it as a text paragraph.
    return { pandoc.Para({ pandoc.Str(el.text) }) }
  elseif t == "Null" then
    return {}
  end
  refuse("block_" .. string.lower(t))
end

rebuild_blocks = function(list)
  local out = {}
  for _, el in ipairs(list) do
    for _, new in ipairs(rebuild_block(el)) do
      out[#out + 1] = new
    end
  end
  return out
end

function Pandoc(doc)
  load_sizes(doc.meta)
  return pandoc.Pandoc(rebuild_blocks(doc.blocks), pandoc.Meta({}))
end
