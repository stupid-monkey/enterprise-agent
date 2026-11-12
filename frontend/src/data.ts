export type Area = '制度规范' | '产品资料' | '项目空间'
export type Visibility = '内部公开' | '部门受限' | '机密'
export type View = 'assistant' | 'library' | 'documents' | 'doc_editor' | 'slides' | 'tasks' | 'access'

export interface LibraryDocument {
  id: string | number
  title: string
  type: 'PDF' | 'DOCX' | 'PPTX' | 'TXT' | 'MD'
  area: Area
  visibility: Visibility
  version: string
  date: string
  owner: string
  excerpt: string
  page: string
  has_original?: boolean
}

export interface WorkTask {
  id: string | number
  title: string
  type: string
  sources: number
  updated: string
  status: '已完成' | '处理中' | '待核对'
}

export const initialDocuments: LibraryDocument[] = [
  { id: 1, title: '差旅报销管理制度', type: 'PDF', area: '制度规范', visibility: '内部公开', version: 'v2.1', date: '10月7日', owner: '行政部 · 王敏', page: '第 3 页 · 审批流程', excerpt: '员工提交报销申请时应填写报销单，经部门负责人审批后，由财务部门审核。财务审核完成后，费用将在 5 个工作日内到账。' },
  { id: 2, title: '员工年假与休假管理办法', type: 'DOCX', area: '制度规范', visibility: '内部公开', version: 'v1.8', date: '10月5日', owner: '人力资源部 · 陈瑜', page: '第 2 页 · 年假标准', excerpt: '工作满 1 年的员工每年享受 5 天年假；工作满 5 年的员工每年享受 10 天年假。' },
  { id: 3, title: '产品 Q4 迭代路线图', type: 'PPTX', area: '产品资料', visibility: '部门受限', version: 'v3.0', date: '10月4日', owner: '产品部 · 林晓宇', page: '第 6 页 · 季度重点', excerpt: '本季度聚焦知识检索、权限治理和文档生成三项能力。第一阶段完成核心工作台，第二阶段开展部门试点。' },
  { id: 4, title: '客户需求汇总与优先级', type: 'DOCX', area: '产品资料', visibility: '部门受限', version: 'v1.2', date: '9月28日', owner: '产品部 · 周宁', page: '第 4 页 · 高优先级需求', excerpt: '高优先级需求包括资料权限分级、结果引用、Word/PPT 模板和生成任务进度。客户期望人工审核生成内容。' },
  { id: 5, title: '产品发布检查清单', type: 'PDF', area: '项目空间', visibility: '机密', version: 'v1.4', date: '9月22日', owner: '项目组 · 刘青', page: '第 1 页 · 发布前检查', excerpt: '发布前需检查功能验收、安全测试、权限回归、备份恢复与回滚方案。发布审批由项目负责人完成。' },
  { id: 6, title: '项目复盘会议纪要', type: 'DOCX', area: '项目空间', visibility: '部门受限', version: 'v1.0', date: '9月18日', owner: '项目组 · 王敏', page: '第 2 页 · 后续行动', excerpt: '项目复盘包括目标完成情况、主要风险、问题原因、下阶段负责人和预定完成时间。' }
]

export const initialTasks: WorkTask[] = [
  { id: 1, title: '差旅报销流程说明', type: '文档整理', sources: 2, updated: '今天 09:42', status: '已完成' },
  { id: 2, title: '产品季度进展汇报', type: '演示文稿', sources: 2, updated: '昨天 16:18', status: '待核对' },
  { id: 3, title: '客户需求与路线图对比', type: '资料对比', sources: 3, updated: '今天 10:24', status: '处理中' }
]

export function answerQuestion(question: string, docs: LibraryDocument[]): { paragraphs: string[]; citations: LibraryDocument[] } {
  const find = (id: number) => docs.find((doc) => doc.id === id)
  if (/报销|差旅|出差/.test(question)) {
    const doc = find(1)
    if (doc) return { paragraphs: ['员工先填写报销单并提交申请；部门负责人审批后，由财务部门审核。', '财务审核完成后，费用将在 5 个工作日内到账。制度未列出其他审批节点。'], citations: [doc] }
  }
  if (/年假|休假/.test(question)) {
    const doc = find(2)
    if (doc) return { paragraphs: ['工作满 1 年的员工每年享受 5 天年假；工作满 5 年的员工每年享受 10 天年假。', '若工作满 3 年，适用每年 5 天这一档。'], citations: [doc] }
  }
  if (/产品|发布|检查/.test(question)) {
    const doc = find(5) ?? find(3)
    if (doc) return { paragraphs: [doc.excerpt], citations: [doc] }
  }
  return { paragraphs: ['当前选定的演示资料中没有找到足够依据。请调整资料范围，或到知识库确认资料是否已发布。'], citations: [] }
}
