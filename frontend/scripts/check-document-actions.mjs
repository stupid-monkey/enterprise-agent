import assert from 'node:assert/strict'
import { createServer } from 'vite'

const server = await createServer({
  server: { middlewareMode: true },
  optimizeDeps: { noDiscovery: true, include: [] }
})
try {
  const { documentActions } = await server.ssrLoadModule('/src/documentActions.ts')
  const cited = { status: 'evidence_ready', citationCount: 1 }
  assert.deepEqual(documentActions('我已经工作了7年，能放多少天年假？', {
    ...cited, paragraphs: ['工作满 1 年享受 5 天年假；工作满 5 年享受 10 天年假。']
  }), { manual: false, generate: false })
  assert.deepEqual(documentActions('请写一份项目进展报告', {
    ...cited, paragraphs: ['本周完成需求评审。']
  }), { manual: true, generate: true })
  assert.deepEqual(documentActions('请根据项目资料准备能给领导看的内容', {
    ...cited, documentRequested: true, paragraphs: ['本周完成需求评审。']
  }), { manual: true, generate: true })
  assert.deepEqual(documentActions('请根据项目资料准备能给领导看的内容', {
    ...cited, documentRequested: false, paragraphs: ['本周完成需求评审。']
  }), { manual: false, generate: false })
  assert.deepEqual(documentActions('请概括这些项目进展', {
    ...cited, paragraphs: ['第一部分。'.repeat(20), '第二部分。']
  }), { manual: true, generate: false })
  assert.deepEqual(documentActions('请生成项目报告', {
    status: 'source_unavailable', citationCount: 0, paragraphs: ['来源已变化。']
  }), { manual: false, generate: false })
  assert.deepEqual(documentActions('请根据项目资料准备能给领导看的内容', {
    status: 'no_evidence', citationCount: 0, documentRequested: true
  }), { manual: false, generate: false })
  assert.deepEqual(documentActions('我有3年工龄，年假多少天？', {
    status: 'answered', citationCount: 1, documentRequested: true
  }), { manual: false, generate: false })
  console.log('Document action visibility checks passed')
} finally {
  await server.close()
}
