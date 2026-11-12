type DocumentAnswer = {
  status?: string
  paragraphs?: string[]
  citationCount: number
  documentRequested?: boolean
}

const DOCUMENT_REQUEST = /(?:撰写|编写|起草|生成|制作|拟一份|写一份|做一份|整理成|汇总成|形成一份).{0,40}(?:文档|报告|周报|月报|纪要|方案|简报|汇报材料|手册|说明书|总结)|(?:文档|报告|周报|月报|纪要|方案|简报|汇报材料|手册|说明书|总结).{0,30}(?:撰写|编写|起草|生成|制作|整理|汇总)/

export function documentActions(prompt: string | undefined, answer: DocumentAnswer): { manual: boolean; generate: boolean } {
  if (!prompt?.trim() || answer.status !== 'evidence_ready' || answer.citationCount === 0) {
    return { manual: false, generate: false }
  }
  const requestedDocument = answer.documentRequested === true || DOCUMENT_REQUEST.test(prompt)
  const paragraphs = answer.paragraphs?.filter(Boolean) ?? []
  const substantialSummary = paragraphs.length >= 2 || paragraphs.join('\n').length >= 180
  return { manual: requestedDocument || substantialSummary, generate: requestedDocument }
}
