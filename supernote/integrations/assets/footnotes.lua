-- footnotes.lua — render footnotes with literal TEXT numbers.
--
-- The Supernote/Manta EPUB reader does not draw <ol> list markers, so pandoc's
-- auto-numbered endnote list shows the note text with no number and no indent
-- (verified on-device: notes render as a stack of bare paragraphs). This filter
-- sidesteps list rendering entirely: each footnote becomes a superscript
-- reference in the body, and an end-of-document notes block is appended where
-- every note begins with its number as plain text ("1. …"), which renders
-- regardless of list-marker support. Anchors are preserved both directions.
--
-- No-op for documents without footnotes. Applies to both the EPUB and the
-- PDF (markdown -> HTML) paths.

local notes = {}

-- Replace each footnote reference with a superscript link; stash its content.
function Note(el)
  local num = #notes + 1
  notes[num] = el.content
  return pandoc.Link(
    { pandoc.Superscript({ pandoc.Str(tostring(num)) }) },
    "#fn" .. num, "",
    pandoc.Attr("fnref" .. num, { "footnote-ref" }, {})
  )
end

-- Append the notes block, numbering each note with literal text.
function Pandoc(doc)
  if #notes == 0 then return nil end
  local items = { pandoc.HorizontalRule() }
  for num, content in ipairs(notes) do
    -- "N." back-link, doubling as the visible (text) number.
    local backlink = pandoc.Link(
      { pandoc.Str(num .. ".") },
      "#fnref" .. num, "",
      pandoc.Attr("fn" .. num, { "footnote-back" }, {})
    )
    local body = {}
    for i, b in ipairs(content) do body[i] = b end
    local first = body[1]
    if first and (first.t == "Para" or first.t == "Plain") then
      local inl = { backlink, pandoc.Space() }
      for _, x in ipairs(first.content) do inl[#inl + 1] = x end
      body[1] = pandoc.Para(inl)
    else
      table.insert(body, 1, pandoc.Para({ backlink }))
    end
    items[#items + 1] = pandoc.Div(body, pandoc.Attr("", { "footnote-item" }, {}))
  end
  table.insert(doc.blocks, pandoc.Div(items, pandoc.Attr("", { "footnotes" }, {})))
  return doc
end
