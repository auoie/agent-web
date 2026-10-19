# Ideas for later

Not planned yet; kept simple for now.

- **Outline mode:** a flag that prints only a page's headings with their offsets, so an agent can
  jump to a section with `--offset`.
- **Section links:** for a URL with a `#fragment`, return just that section instead of the page
  from the top.
- **PDF tables:** pymupdf garbles some tables (wrong columns on the Attention paper's Table 2). An
  opt-in engine such as Docling would be more accurate but is heavy (GBs of models). For now, have
  the agent use a PDF-specific tool when tables matter.
- **Large PDFs and books:** downloads over 25 MB are refused, and a 700-page book takes ~70 s to
  convert. Would need a separate size limit and reading by page range.
- **Hidden text in PDFs:** invisible, white and tiny text is dropped rather than labeled.
- **Other formats:** Office files and EPUB aren't converted.

Research behind these: `claude-efficiency/agent-web-alternatives.md`.
