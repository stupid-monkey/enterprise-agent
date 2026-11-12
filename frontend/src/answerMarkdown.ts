import MarkdownIt from 'markdown-it'

const markdown = new MarkdownIt({ html: false, breaks: true, linkify: false })

markdown.renderer.rules.image = (tokens, index) => markdown.utils.escapeHtml(tokens[index].content)
markdown.renderer.rules.link_open = (tokens, index, options, environment, self) => {
  tokens[index].attrSet('target', '_blank')
  tokens[index].attrSet('rel', 'noopener noreferrer')
  return self.renderToken(tokens, index, options)
}

export function renderGeneralAnswer(paragraphs: string[] | undefined): string {
  const text = (paragraphs ?? []).join('\n\n')
  // Some compatible model endpoints emit a fenced code sample on one line.
  const readable = text.replace(/```([A-Za-z][\w+-]*)[ \t]+([^\r\n]*?)```/g,
    '\n\n```$1\n$2\n```\n\n')
  return markdown.render(readable)
}
