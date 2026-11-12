import assert from 'node:assert/strict'
import { createServer } from 'vite'

const server = await createServer({
  server: { middlewareMode: true },
  optimizeDeps: { noDiscovery: true, include: [] }
})
try {
  const { renderGeneralAnswer } = await server.ssrLoadModule('/src/answerMarkdown.ts')
  const compact = renderGeneralAnswer([
    '最常用的是 `with open()`： ```python with open("hello.txt", "w") as f: f.write("你好") ``` `w` 会覆盖内容。'
  ])
  assert.match(compact, /<pre><code class="language-python">/)
  assert.match(compact, /<code>with open\(\)<\/code>/)
  assert.match(compact, /<code>w<\/code>/)
  assert.doesNotMatch(compact, /```/)

  const structured = renderGeneralAnswer(['## 写文件\n\n1. 打开文件\n2. 写入内容\n\n**完成**'])
  assert.match(structured, /<h2>写文件<\/h2>/)
  assert.match(structured, /<ol>/)
  assert.match(structured, /<strong>完成<\/strong>/)

  const unsafe = renderGeneralAnswer([
    '<script>alert(1)</script> ![远程图](https://example.com/tracker.png) [危险](javascript:alert(1))'
  ])
  assert.doesNotMatch(unsafe, /<script|<img|href="javascript:/)
  assert.match(unsafe, /&lt;script&gt;/)
  console.log('Answer Markdown rendering checks passed')
} finally {
  await server.close()
}
