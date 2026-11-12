import type { LibraryDocument } from './data'

export type CurrentUser = {
  user_id: string
  org_id: string
  department_ids: string[]
  clearance: string
  role: 'employee' | 'department_manager' | 'knowledge_admin' | 'system_admin'
}

export type MeetingRoom = { id: string; org_id: string; department_id: string; title: string;
  owner_id: string; created_at: string; updated_at?: string; last_message?: string | null;
  member_count?: number; members?: MeetingMember[] }
export type MeetingMember = { user_id: string; username: string; added_by: string; joined_at: string }
export type MeetingCandidate = { user_id: string; username: string; role: CurrentUser['role']; department_ids: string[] }
export type MeetingMessage = { id: number; room_id: string; sender_id: string; text: string; created_at: string }
export type MeetingMinuteItem = { message_id: number; quote: string; sender_id: string; created_at: string }
export type MeetingMinutes = { room_id: string; source_count: number; first_message_id: number;
  last_message_id: number; truncated: boolean; discussion: MeetingMinuteItem[];
  decisions: MeetingMinuteItem[]; actions: MeetingMinuteItem[] }

export type MemoryKind = 'role' | 'task' | 'answer_preference'
export type MemoryEntry = { kind: MemoryKind; value: string; created_at: string; updated_at: string }
export type MemorySettings = { auto_suggest: boolean }
export type MemorySuggestion = { id: string; kind: MemoryKind; value: string; source_conversation_id: string; source_text: string; created_at: string }

type LoginResponse = { access_token: string; token_type: 'bearer' }
type ApiError = { status: 'error'; code: string; trace_id: string }

export class AnswerStreamError extends Error {
  constructor(readonly code: string, message: string) { super(message) }
}

const tokenKey = 'enterprise-agent-demo-token'

export function savedToken(): string | null {
  return sessionStorage.getItem(tokenKey)
}

export function clearToken(): void {
  sessionStorage.removeItem(tokenKey)
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(`/api/v1${path}`, {
      ...init,
      headers: { ...(init.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), ...init.headers }
    })
  } catch {
    throw new Error('无法连接服务，请检查 API 是否已启动')
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null) as ApiError | null
    if (response.status === 401 && path !== '/auth/login') clearToken()
    const message = body?.code === 'DOCUMENT_NOT_FOUND' ? '资料已下线或当前账号无权访问' :
      body?.code === 'INVALID_CREDENTIALS' ? '账号或密码错误' :
      body?.code === 'AUTH_NOT_CONFIGURED' ? '开发账号尚未配置' :
      body?.code === 'INVALID_FILE' ? '文件格式或大小不符合要求（最大 8 MB）' :
      body?.code === 'UNREADABLE_FILE' ? '文件没有可提取的文字内容' :
      body?.code === 'SOURCE_NOT_ALLOWED' ? '来源资料已撤权或不可用于生成' :
      body?.code === 'COMPOSITION_NO_EVIDENCE' ? '当前可用于生成的资料中没有匹配依据，请调整问题或资料权限' :
      body?.code === 'COMPOSITION_PLAN_INVALID' ? '文档规划结果未通过校验，请稍后重试' :
      body?.code === 'COMPOSITION_INPUT_TOO_LARGE' ? '问题及候选资料过长，请缩小整理范围后重试' :
      body?.code === 'COMPOSITION_TIME_BUDGET_EXCEEDED' ? '文档规划超时，请稍后重试' :
      body?.code === 'COMPOSITION_REQUEST_CONFLICT' ? '同一请求编号对应的问题已改变，请重新发起' :
      body?.code === 'SOURCE_VERSION_CHANGED' ? path === '/artifacts/compose-run' ? '来源资料版本已变化，请重新提问后生成草稿' : '来源资料版本已变化，请重新创建任务' :
      body?.code === 'DRAFT_CHANGED' ? '草稿已变化，请重新打开任务核对后再保存' :
      body?.code === 'INVALID_NOTE' ? '人工补充含无效字符或超过长度限制' :
      body?.code === 'INVALID_EDIT' ? '无法从这份草稿生成有依据的修改，请明确章节和修改内容' :
      body?.code === 'EDIT_SOURCE_TOO_LARGE' ? '文件正文过长，当前只支持最多 10 个内容片段；请先缩小文件范围' :
      body?.code === 'REVIEW_REQUIRED' ? '请先保存每个主题的人工核对结论' :
      body?.code === 'INVALID_REVIEW' ? '核对结论不完整或与证据缺口不符' :
      body?.code === 'REVISION_PENDING' ? '这份资料已有待审核版本' :
      body?.code === 'FORBIDDEN' ? '当前角色没有此操作权限' :
      body?.code === 'ROOM_NOT_FOUND' ? '会议室已不可访问，请刷新列表' :
      body?.code === 'ROOM_ALREADY_EXISTS' ? '该部门已有会议室' :
      body?.code === 'MINUTES_NO_MESSAGES' ? '会议室还没有消息，无法生成纪要' :
      body?.code === 'MINUTES_NOT_CONFIGURED' ? '尚未配置模型，无法生成纪要' :
      body?.code === 'MINUTES_MODEL_INVALID' ? '纪要依据校验失败，请稍后重试' :
      body?.code === 'MINUTES_MODEL_UNAVAILABLE' ? '纪要模型暂时不可用，请稍后重试' :
      body?.code === 'ACCOUNT_NOT_FOUND' ? '该账号不存在或不在当前组织' :
      body?.code === 'GPT_NOT_CONFIGURED' ? 'GPT 引句功能当前未启用' :
      body?.code === 'MODEL_UNSUPPORTED_QUOTE' ? '模型引句未通过来源校验，请改用原文摘录' :
      body?.code === 'MODEL_NO_STRUCTURED_OUTPUT' ? '模型未返回有效引句，请改用原文摘录' :
      body?.code === 'MODEL_UNAVAILABLE' ? path === '/artifacts/compose-run' ? '文档规划模型暂时不可用，请稍后重试' : '模型暂时不可用，请改用原文摘录' :
      body?.code === 'COLLABORATION_NOT_CONFIGURED' ? '未配置模型，暂不能运行 AI 协作整理' :
      body?.code === 'COLLABORATION_UNSUPPORTED' ? '此任务类型暂不支持 AI 协作整理' :
      body?.code === 'COLLABORATION_RETRY_LIMIT' ? '本轮 AI 协作已达重试上限，请重新创建任务' :
      body?.code === 'COLLABORATION_CALL_BUDGET_EXCEEDED' ? '本轮 AI 协作调用次数已达上限，草稿未更新' :
      body?.code === 'COLLABORATION_TIME_BUDGET_EXCEEDED' ? '本轮 AI 协作运行超时，草稿未更新' :
      body?.code === 'COLLABORATION_INPUT_TOO_LARGE' ? '来源摘录过长，请减少章节或资料后重试' :
      body?.code === 'NO_EVIDENCE' ? '各章节均缺少匹配证据，请调整提纲或来源' :
      body?.code === 'MODEL_OUTPUT_INVALID' ? '模型输出格式不符合要求，草稿未更新' :
        response.status === 401 ? '登录已失效，请重新登录' : '服务暂时不可用'
    throw new Error(message)
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>
}

export function listMeetingRooms(): Promise<MeetingRoom[]> {
  return request('/meeting-rooms', { headers: authHeaders() })
}

export function getMeetingRoom(id: string): Promise<MeetingRoom> {
  return request(`/meeting-rooms/${encodeURIComponent(id)}`, { headers: authHeaders() })
}

export function createMeetingRoom(departmentId: string, title: string): Promise<MeetingRoom> {
  return request('/meeting-rooms', { method: 'POST', headers: authHeaders(),
    body: JSON.stringify({ department_id: departmentId, title }) })
}

export function listMeetingCandidates(): Promise<MeetingCandidate[]> {
  return request('/meeting-rooms/candidates', { headers: authHeaders() })
}

export function addMeetingMember(id: string, userId: string): Promise<MeetingRoom> {
  return request(`/meeting-rooms/${encodeURIComponent(id)}/members`, { method: 'POST', headers: authHeaders(),
    body: JSON.stringify({ user_id: userId }) })
}

export function removeMeetingMember(id: string, userId: string): Promise<void> {
  return request(`/meeting-rooms/${encodeURIComponent(id)}/members/${encodeURIComponent(userId)}`,
    { method: 'DELETE', headers: authHeaders() })
}

export function listMeetingMessages(id: string, afterId = 0, beforeId?: number): Promise<MeetingMessage[]> {
  const query = beforeId ? `before_id=${beforeId}` : `after_id=${afterId}`
  return request(`/meeting-rooms/${encodeURIComponent(id)}/messages?${query}`, { headers: authHeaders() })
}

export function postMeetingMessage(id: string, text: string): Promise<MeetingMessage> {
  return request(`/meeting-rooms/${encodeURIComponent(id)}/messages`, { method: 'POST', headers: authHeaders(),
    body: JSON.stringify({ text }) })
}

export function draftMeetingMinutes(id: string): Promise<MeetingMinutes> {
  return request(`/meeting-rooms/${encodeURIComponent(id)}/minutes`,
    { method: 'POST', headers: authHeaders() })
}

export async function login(username: string, password: string): Promise<CurrentUser> {
  const { access_token } = await request<LoginResponse>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password })
  })
  try {
    const user = await getCurrentUser(access_token)
    sessionStorage.setItem(tokenKey, access_token)
    return user
  } catch (error) {
    clearToken()
    throw error
  }
}

export function getCurrentUser(token = savedToken()): Promise<CurrentUser> {
  if (!token) return Promise.reject(new Error('请先登录'))
  return request<CurrentUser>('/me', { headers: { Authorization: `Bearer ${token}` } })
}

export function listMemories(): Promise<MemoryEntry[]> {
  return request('/me/memories', { headers: authHeaders() })
}

export function getMemorySettings(): Promise<MemorySettings> {
  return request('/me/memory-settings', { headers: authHeaders() })
}

export function updateMemorySettings(auto_suggest: boolean): Promise<MemorySettings> {
  return request('/me/memory-settings', {
    method: 'PUT', headers: authHeaders(), body: JSON.stringify({ auto_suggest })
  })
}

export function listMemorySuggestions(): Promise<MemorySuggestion[]> {
  return request('/me/memory-suggestions', { headers: authHeaders() })
}

export function acceptMemorySuggestion(id: string, value: string): Promise<MemoryEntry> {
  return request(`/me/memory-suggestions/${encodeURIComponent(id)}/accept`, {
    method: 'POST', headers: authHeaders(), body: JSON.stringify({ value })
  })
}

export function rejectMemorySuggestion(id: string): Promise<void> {
  return request(`/me/memory-suggestions/${encodeURIComponent(id)}`, {
    method: 'DELETE', headers: authHeaders()
  })
}

export function saveMemory(kind: MemoryKind, value: string): Promise<MemoryEntry> {
  return request(`/me/memories/${kind}`, {
    method: 'PUT', headers: authHeaders(), body: JSON.stringify({ value })
  })
}

export function deleteMemory(kind: MemoryKind): Promise<void> {
  return request(`/me/memories/${kind}`, { method: 'DELETE', headers: authHeaders() })
}

export async function logout(): Promise<void> {
  const token = savedToken()
  clearToken()
  if (!token) return
  try {
    await request<void>('/auth/logout', { method: 'POST', headers: { Authorization: `Bearer ${token}` } })
  } catch {
    // The local session is cleared even if the server has already restarted.
  }
}

type ApiDocument = Omit<LibraryDocument, 'id' | 'excerpt' | 'page'> & {
  id: string
  excerpt?: string
  page?: string
}

function asLibraryDocument(document: ApiDocument): LibraryDocument {
  return { ...document, id: document.id, excerpt: document.excerpt ?? '', page: document.page ?? '' }
}

export async function listDocuments(): Promise<LibraryDocument[]> {
  const token = savedToken()
  if (!token) throw new Error('请先登录')
  const documents = await request<ApiDocument[]>('/documents', { headers: { Authorization: `Bearer ${token}` } })
  return documents.map(asLibraryDocument)
}

export async function getDocument(id: string | number): Promise<LibraryDocument> {
  const token = savedToken()
  if (!token) throw new Error('请先登录')
  const document = await request<ApiDocument>(`/documents/${id}`, { headers: { Authorization: `Bearer ${token}` } })
  return asLibraryDocument(document)
}

export type DocumentPreview = LibraryDocument & { preview_text: string; truncated: boolean }

export async function getDocumentPreview(id: string | number): Promise<DocumentPreview> {
  const document = await request<ApiDocument & { preview_text: string; truncated: boolean }>(
    `/documents/${encodeURIComponent(id)}/preview`, { headers: authHeaders() })
  return { ...asLibraryDocument(document), preview_text: document.preview_text, truncated: document.truncated }
}

export async function downloadDocumentOriginal(id: string | number, version: string): Promise<void> {
  let response: Response
  try {
    response = await fetch(`/api/v1/documents/${encodeURIComponent(id)}/original?version=${encodeURIComponent(version)}`, { headers: authHeaders() })
  } catch {
    throw new Error('无法连接服务，请检查 API 是否已启动')
  }
  if (!response.ok) {
    if (response.status === 401) clearToken()
    throw new Error(response.status === 404 ? '资料已下线、无原件或当前账号无下载权限' :
      response.status === 409 ? '资料版本已更新，请重新打开详情下载' : '原件下载失败，请稍后重试')
  }
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1]
  let filename = `document-${id}`
  if (encodedName) {
    try { filename = decodeURIComponent(encodedName) } catch { /* retain fallback */ }
  }
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

export type PendingDocument = Omit<LibraryDocument, 'excerpt' | 'page'>
export type PendingVersion = { document_id: string; version: string; title: string }
export type Citation = { document_id: string; title: string; version: string; location: string; excerpt?: string }
export type EditProposal = { id: string; artifact_id: string; artifact_title: string; kind: 'docx' | 'pptx'; section_index: number; heading: string; before_text: string; revised_text: string; source_quote: string; status: 'pending' | 'applied' | 'stale' | 'outdated'; draft_token: string }
export type Clarification = { field: string; prompt: string; input_type: 'number' | 'select'; unit: string; options: string[]; original_question: string }
export type Answer = { status: 'evidence_ready' | 'answered' | 'needs_clarification' | 'no_evidence' | 'source_unavailable' | 'general' | 'file_access' | 'edit_proposed'; paragraphs: string[]; citations: Citation[]; document_requested?: boolean; edit?: EditProposal | null; clarification?: Clarification | null }
export type ConversationSummary = { id: string; title: string; updated_at: string; turn_count: number }
export type ConversationDetail = { id: string; title: string; updated_at: string; turns: { question: string; mode: 'extract' | 'gpt' | 'general' | 'file_access' | 'artifact_edit'; answer: Answer }[] }
export type Capabilities = { gpt_quotes: boolean; collaboration: boolean; reflection: boolean; online_slide_images: boolean }
export type ComparisonSource = { document_id: string; title: string; version: string; text: string; evidence_gap: boolean; citation: Citation | null }
export type ComparisonReview = { heading: string; decision: 'aligned' | 'different' | 'unresolved'; note: string }
export type SavedComparisonReview = Omit<ComparisonReview, 'heading'> & { reviewed_by: string; reviewed_at: string }
export type SectionDraft = { heading: string; text: string; evidence_gap: boolean; citation: Citation | null; supporting_sources?: { text: string; citation: Citation }[]; ai_quote?: string; ai_text?: string; ai_evidence?: { quote: string; citation: Citation }[]; manual_note?: string; manual_note_by?: string; sources?: ComparisonSource[]; comparison_status?: 'missing_evidence' | 'review_required'; review?: SavedComparisonReview }
export type CollaborationStep = { agent: string; elapsed_ms: number; input_sections: number; output: Record<string, unknown> }
export type CollaborationRun = { id: string; request_id: string; draft_token: string; status: 'pending' | 'failed' | 'completed'; stage: string | null; error_code: string | null; trace: CollaborationStep[]; attempts: number; max_attempts: number; can_retry: boolean }
export type CompoundTask = { artifact: Artifact; run: CollaborationRun | null; status: 'completed' | 'failed' | 'pending' | 'not_run' }
export type ReflectionStep = { round?: number; agent?: string; elapsed_ms?: number; issue_count?: number; issue_sections?: number[]; severities?: string[]; revised_sections?: number[]; kind?: string; status?: string; reason?: string; model_calls?: number }
export type SlideVisual = { page_index: number; category: string; source: 'document' | 'online' | 'local'; asset_name: string; photographer: string | null; page_url: string | null; license_url: string | null; source_document_id: string | null; source_version: string | null; source_title: string | null; source_location: string | null }
export type Artifact = { id: string; kind: 'docx' | 'pptx'; title: string; goal: string; brand_name: string; theme: 'brief' | 'strategy'; mode: 'standard' | 'comparison'; source_ids: string[]; source_versions: Record<string, string> | null; source_status: 'current' | 'stale' | 'unverified'; outline: string[]; section_drafts: SectionDraft[]; slide_visuals: SlideVisual[]; draft_token: string; collaboration_trace: CollaborationStep[]; reflection_trace: ReflectionStep[]; status: 'pending' | 'completed'; composition_created?: boolean }
export type AuditEvent = { id: number; occurred_at: string; org_id: string; actor_id: string; action: string; resource_type: string; resource_id: string; outcome: 'success' | 'denied'; trace_id: string }

function authHeaders(): HeadersInit {
  const token = savedToken()
  if (!token) throw new Error('请先登录')
  return { Authorization: `Bearer ${token}` }
}

export async function uploadDocument(file: File, title: string, area: string, visibility: string): Promise<{ id: string }> {
  const form = new FormData()
  form.append('file', file)
  form.append('title', title)
  form.append('area', area)
  form.append('visibility', visibility)
  return request('/documents/upload', { method: 'POST', body: form, headers: authHeaders() })
}

export function listPendingDocuments(): Promise<PendingDocument[]> {
  return request('/documents/pending/review', { headers: authHeaders() })
}

export function listPendingVersions(): Promise<PendingVersion[]> {
  return request('/documents/versions/pending', { headers: authHeaders() })
}

export function uploadDocumentVersion(id: string | number, file: File): Promise<{ id: string; version: string }> {
  const form = new FormData()
  form.append('file', file)
  return request(`/documents/${encodeURIComponent(id)}/versions`, {
    method: 'POST', body: form, headers: authHeaders()
  })
}

export function publishDocumentVersion(id: string | number): Promise<void> {
  return request(`/documents/${encodeURIComponent(id)}/versions/publish`, {
    method: 'POST', headers: authHeaders()
  })
}

export function publishDocument(id: string | number): Promise<void> {
  return request(`/documents/${id}/publish`, { method: 'POST', headers: authHeaders() })
}

export function getCapabilities(): Promise<Capabilities> {
  return request('/capabilities', { headers: authHeaders() })
}

export function askQuestion(question: string, area: string | null, mode: 'extract' | 'gpt' = 'extract'): Promise<Answer> {
  return request(mode === 'gpt' ? '/answers/gpt' : '/answers/workflow', {
    method: 'POST', headers: authHeaders(), body: JSON.stringify({ question, area })
  })
}

export function listConversations(): Promise<ConversationSummary[]> {
  return request('/conversations', { headers: authHeaders() })
}

export function getConversation(id: string): Promise<ConversationDetail> {
  return request(`/conversations/${encodeURIComponent(id)}`, { headers: authHeaders() })
}

export function deleteConversation(id: string): Promise<void> {
  return request(`/conversations/${encodeURIComponent(id)}`, {
    method: 'DELETE', headers: authHeaders()
  })
}

export function getWorkflowConversation(runId: string): Promise<{ conversation_id: string | null }> {
  return request(`/conversations/workflow-runs/${encodeURIComponent(runId)}`, { headers: authHeaders() })
}

export function askInConversation(question: string, area: string | null, mode: 'extract' | 'gpt', conversationId: string | null): Promise<{ conversation_id: string; answer: Answer }> {
  return request('/conversations/answers', {
    method: 'POST', headers: authHeaders(), body: JSON.stringify({ question, area, mode, conversation_id: conversationId })
  })
}

type StreamEvent =
  | { type: 'meta'; intent: 'knowledge' | 'general' | 'file_access' | 'file_answer' | 'edit'; workflow_run_id: string | null }
  | { type: 'delta'; text: string }
  | { type: 'done'; conversation_id: string; answer: Answer; intent: 'knowledge' | 'general' | 'file_access' | 'file_answer' | 'edit' }
  | { type: 'error'; code: string }

export async function streamConversationAnswer(
  question: string, area: string | null, mode: 'extract' | 'gpt', conversationId: string | null,
  onEvent: (event: StreamEvent) => void, signal?: AbortSignal, workflowRunId?: string | null,
  target?: { artifact_id: string } | { source_document_id: string }
): Promise<void> {
  const response = await fetch('/api/v1/conversations/answers/stream', {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, signal,
    body: JSON.stringify({ question, area, mode, conversation_id: conversationId,
      workflow_run_id: workflowRunId ?? null, ...target })
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null) as ApiError | null
    if (response.status === 401) clearToken()
    throw new AnswerStreamError(body?.code ?? 'ANSWER_REQUEST_FAILED', body?.code === 'GENERAL_CHAT_NOT_CONFIGURED' ? '普通对话需要配置 GPT 服务' :
      body?.code === 'MODEL_AUTH_FAILED' ? 'GPT API 密钥无效，请检查服务端配置' :
      body?.code === 'CONVERSATION_NOT_FOUND' ? '会话已不可用，请新建会话' :
      body?.code === 'WORKFLOW_NOT_FOUND' ? '运行已不可用，请重新提问' :
      body?.code === 'DOCUMENT_NOT_FOUND' || body?.code === 'ARTIFACT_NOT_FOUND' ? '文件已不可用，请重新选择' :
      body?.code === 'SOURCE_VERSION_CHANGED' ? '文件版本已变化，请重新选择' :
      body?.code === 'EDIT_SOURCE_TOO_LARGE' ? '文件过长，当前无法生成修改预览' :
      body?.code === 'INVALID_EDIT' ? '未能生成可靠的修改预览，请调整要求后重试' :
      body?.code === 'COLLABORATION_NOT_CONFIGURED' ? '文件修改需要配置模型服务' :
      response.status === 401 ? '登录已失效，请重新登录' : '回答请求失败')
  }
  if (!response.body) throw new Error('当前浏览器不支持流式回答')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let completed = false
  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      const frames = buffer.split('\n\n')
      buffer = frames.pop() ?? ''
      for (const frame of frames) {
        const data = frame.split('\n').find(line => line.startsWith('data: '))?.slice(6)
        if (!data) continue
        const event = JSON.parse(data) as StreamEvent
        if (event.type === 'error') throw new AnswerStreamError(event.code, event.code === 'MODEL_AUTH_FAILED' ?
          'GPT API 密钥无效，请检查服务端配置' : event.code === 'SOURCE_UNAVAILABLE' ?
          '文件权限或版本已变化，请重新选择' : '回答中断，请重试')
        onEvent(event)
        if (event.type === 'done') completed = true
      }
      if (done) break
    }
    if (!completed) throw new Error('连接中断，回答未保存，请重试')
  } finally {
    reader.releaseLock()
  }
}

export function listArtifacts(): Promise<Artifact[]> {
  return request('/artifacts', { headers: authHeaders() })
}

export function getArtifact(id: string): Promise<Artifact> {
  return request(`/artifacts/${encodeURIComponent(id)}`, { headers: authHeaders() })
}

export function proposeArtifactEdit(target: { artifact_id: string } | { source_document_id: string }, instruction: string, conversation_id: string | null): Promise<{ conversation_id: string; proposal: EditProposal }> {
  return request('/conversations/artifact-edits', { method: 'POST', headers: authHeaders(),
    body: JSON.stringify({ ...target, instruction, conversation_id }) })
}

export function confirmArtifactEdit(proposalId: string): Promise<Artifact> {
  return request(`/artifact-edit-proposals/${encodeURIComponent(proposalId)}/confirm`,
    { method: 'POST', headers: authHeaders() })
}

export function listAuditEvents(): Promise<AuditEvent[]> {
  return request('/admin/audit-events', { headers: authHeaders() })
}

export function createArtifact(kind: Artifact['kind'], title: string, goal: string, source_ids: string[], outline: string[], theme: Artifact['theme'] = 'brief', mode: Artifact['mode'] = 'standard'): Promise<Artifact> {
  return request('/artifacts', { method: 'POST', headers: authHeaders(), body: JSON.stringify({ kind, title, goal, source_ids, outline, theme, mode }) })
}

export function suggestSlideOutline(source_ids: string[], count: number): Promise<{ outline: string[] }> {
  return request('/artifacts/slide-outline', {
    method: 'POST', headers: authHeaders(), body: JSON.stringify({ source_ids, count })
  })
}

export function composeArtifactFromQuestion(question: string, request_id: string): Promise<Artifact> {
  return request('/artifacts/compose', {
    method: 'POST', headers: authHeaders(), body: JSON.stringify({ question, request_id })
  })
}

export function composeAndRunFromQuestion(question: string, request_id: string,
  answer?: { conversation_id: string; turn_index: number }): Promise<CompoundTask> {
  return request('/artifacts/compose-run', {
    method: 'POST', headers: authHeaders(), body: JSON.stringify({ question, request_id, ...answer })
  })
}

export function confirmArtifact(id: string, outline: string[], draft_token: string): Promise<Artifact> {
  return request(`/artifacts/${id}/confirm`, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ outline, draft_token }) })
}

export function previewArtifact(id: string, outline: string[]): Promise<Artifact> {
  return request(`/artifacts/${id}/preview`, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ outline }) })
}

export function collaborateArtifact(id: string, draft_token: string, request_id: string): Promise<Artifact> {
  return request(`/artifacts/${id}/collaborate`, {
    method: 'POST', headers: authHeaders(), body: JSON.stringify({ draft_token, request_id })
  })
}

export function generateSlideVisuals(id: string, draft_token: string): Promise<Artifact> {
  return request(`/artifacts/${encodeURIComponent(id)}/slide-visuals`, {
    method: 'POST', headers: authHeaders(), body: JSON.stringify({ draft_token })
  })
}

export async function getSlideVisualImage(id: string, pageIndex: number, signal?: AbortSignal): Promise<Blob> {
  const response = await fetch(`/api/v1/artifacts/${encodeURIComponent(id)}/slide-visuals/${pageIndex}/image`,
    { headers: authHeaders(), signal, cache: 'no-store' })
  if (!response.ok) throw new Error('配图已不可用，请重新匹配')
  return response.blob()
}

export function reflectArtifact(id: string, draft_token: string, request_id: string): Promise<Artifact> {
  return request(`/artifacts/${id}/reflect`, {
    method: 'POST', headers: authHeaders(), body: JSON.stringify({ draft_token, request_id })
  })
}

export function getCollaborationRun(id: string): Promise<CollaborationRun> {
  return request(`/artifacts/${id}/collaboration-run`, { headers: authHeaders() })
}

export function reviewArtifact(id: string, reviews: ComparisonReview[]): Promise<Artifact> {
  return request(`/artifacts/${id}/review`, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ reviews }) })
}

export function saveArtifactNotes(id: string, draft_token: string, notes: { heading: string; text: string }[]): Promise<Artifact> {
  return request(`/artifacts/${id}/notes`, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ draft_token, notes }) })
}

export async function downloadArtifact(id: string, kind: Artifact['kind']): Promise<void> {
  const response = await fetch(`/api/v1/artifacts/${id}/download`, { headers: authHeaders() })
  if (!response.ok) {
    const body = await response.json().catch(() => null) as ApiError | null
    throw new Error(body?.code === 'SOURCE_VERSION_CHANGED' ? '来源资料版本已变化，请重新创建任务' : '文件不可下载，请检查来源权限')
  }
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url
  link.download = `${id}.${kind}`
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}
