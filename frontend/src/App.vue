<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  ArrowDownToLine, ArrowRight, ArrowUp, Bell, BookOpenText, Check,
  ChevronDown, ChevronLeft, ChevronRight, Clock3, FileCheck2,
  Eye, EyeOff, FilePlus2, FileText, Files, ImagePlus, LayoutList, LockKeyhole,
  LoaderCircle, Menu, MessageSquareText, MoreHorizontal, Plus, Presentation, Search,
  ShieldCheck, SlidersHorizontal, Sparkles, Trash2, Upload, Users, X, LogOut
} from 'lucide-vue-next'
import { answerQuestion, initialDocuments, initialTasks, type LibraryDocument, type View, type WorkTask } from './data'
import { AnswerStreamError, clearToken, collaborateArtifact, composeAndRunFromQuestion, confirmArtifact, confirmArtifactEdit, createArtifact, deleteConversation, downloadArtifact, downloadDocumentOriginal, generateSlideVisuals, getArtifact, getCapabilities, getCollaborationRun, getConversation, getCurrentUser, getDocument, getDocumentPreview, getSlideVisualImage, getWorkflowConversation, listArtifacts, listAuditEvents, listConversations, listDocuments, listPendingDocuments, listPendingVersions, login, logout, previewArtifact, publishDocument, publishDocumentVersion, reflectArtifact, reviewArtifact, saveArtifactNotes, savedToken, streamConversationAnswer, suggestSlideOutline, uploadDocument, uploadDocumentVersion, type Artifact, type AuditEvent, type Citation, type Clarification, type CollaborationRun, type ComparisonReview, type ConversationSummary, type CurrentUser, type DocumentPreview, type EditProposal, type PendingDocument, type PendingVersion } from './auth'
import AgentCollaborationTimeline from './AgentCollaborationTimeline.vue'
import DepartmentMeeting from './DepartmentMeeting.vue'
import ListPager from './ListPager.vue'
import { renderGeneralAnswer } from './answerMarkdown'
import { documentActions } from './documentActions'

const sessionState = ref<'loading' | 'login' | 'ready' | 'demo'>('loading')
const currentUser = ref<CurrentUser | null>(null)
type ConversationSyncEvent = { scope: string; action: 'upsert' | 'delete'; conversationId: string; sender: string }
const conversationSyncChannelName = 'enterprise-agent-conversations-v1'
const conversationSyncStorageKey = 'enterprise-agent-conversations-event-v1'
const conversationSyncSender = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`
let conversationSyncChannel: BroadcastChannel | null = null
let pendingConversationSync: string | null = null
let conversationSyncSequence = 0
function conversationScope(): string | null {
  return currentUser.value ? `${currentUser.value.org_id}:${currentUser.value.user_id}` : null
}
function publishConversationSync(action: ConversationSyncEvent['action'], conversationId: string) {
  const scope = conversationScope()
  if (!scope) return
  const event: ConversationSyncEvent = { scope, action, conversationId, sender: conversationSyncSender }
  conversationSyncChannel?.postMessage(event)
  try { localStorage.setItem(conversationSyncStorageKey, JSON.stringify(event)) } catch { /* Storage can be unavailable. */ }
}
function validConversationSyncEvent(value: unknown): value is ConversationSyncEvent {
  if (!value || typeof value !== 'object') return false
  const event = value as Partial<ConversationSyncEvent>
  return event.scope === conversationScope() && event.sender !== conversationSyncSender &&
    (event.action === 'upsert' || event.action === 'delete') &&
    typeof event.conversationId === 'string' && event.conversationId.length > 0
}
async function receiveConversationSync(value: unknown) {
  if (sessionState.value !== 'ready' || !validConversationSyncEvent(value)) return
  const event = value
  if (event.action === 'delete' && activeConversationId.value === event.conversationId) {
    const draft = question.value
    answerAbort?.abort()
    answerAbort = null
    questionBusy.value = false
    newChat()
    question.value = draft
    notify('当前会话已在另一标签页删除')
  }
  if (event.action === 'upsert' && activeConversationId.value === event.conversationId) {
    if (questionBusy.value || conversationLoading.value) pendingConversationSync = event.conversationId
    else void syncActiveConversation(event.conversationId)
  }
  await refreshConversations()
}
function onConversationStorage(event: StorageEvent) {
  if (event.key !== conversationSyncStorageKey || !event.newValue) return
  try { void receiveConversationSync(JSON.parse(event.newValue)) } catch { /* Ignore malformed events. */ }
}
async function reconcileConversationsOnFocus() {
  if (sessionState.value !== 'ready') return
  const scope = conversationScope()
  const token = savedToken()
  try {
    const items = await listConversations()
    if (scope !== conversationScope() || token !== savedToken() || sessionState.value !== 'ready') return
    conversationListSequence++
    conversations.value = items
    const active = activeConversationId.value
    if (active && !items.some(item => item.id === active)) {
      const draft = question.value
      answerAbort?.abort()
      answerAbort = null
      questionBusy.value = false
      newChat()
      question.value = draft
      notify('当前会话已删除')
    } else if (active && !questionBusy.value && !conversationLoading.value) {
      void syncActiveConversation(active)
    }
  } catch { /* The existing list stays available while offline. */ }
}
function onConversationFocus() { void reconcileConversationsOnFocus() }
function onConversationVisibility() { if (!document.hidden) void reconcileConversationsOnFocus() }
onMounted(() => {
  if (typeof BroadcastChannel !== 'undefined') {
    conversationSyncChannel = new BroadcastChannel(conversationSyncChannelName)
    conversationSyncChannel.onmessage = event => { void receiveConversationSync(event.data) }
  }
  window.addEventListener('storage', onConversationStorage)
  window.addEventListener('focus', onConversationFocus)
  document.addEventListener('visibilitychange', onConversationVisibility)
})
onUnmounted(() => {
  conversationSyncChannel?.close()
  window.removeEventListener('storage', onConversationStorage)
  window.removeEventListener('focus', onConversationFocus)
  document.removeEventListener('visibilitychange', onConversationVisibility)
})
const username = ref('')
const password = ref('')
const showPassword = ref(false)
const loginError = ref('')
const loginBusy = ref(false)
const roleNames: Record<CurrentUser['role'], string> = {
  employee: '普通员工', department_manager: '部门负责人',
  knowledge_admin: '知识管理员', system_admin: '系统管理员'
}

onMounted(async () => {
  if (!savedToken()) { sessionState.value = 'login'; return }
  try {
    currentUser.value = await getCurrentUser()
    sessionState.value = 'ready'
    messages.value = []
    void refreshDocuments()
    void refreshWorkspace()
    void refreshCapabilities()
    void refreshAuditEvents()
    void restoreActiveConversation()
  } catch {
    clearToken()
    sessionState.value = 'login'
  }
})

async function submitLogin() {
  loginError.value = ''
  loginBusy.value = true
  try {
    currentUser.value = await login(username.value.trim(), password.value)
    password.value = ''
    showPassword.value = false
    sessionState.value = 'ready'
    messages.value = []
    void refreshDocuments()
    void refreshWorkspace()
    void refreshCapabilities()
    void refreshAuditEvents()
    void restoreActiveConversation()
  } catch (error) {
    loginError.value = error instanceof Error ? error.message : '登录失败'
  } finally {
    loginBusy.value = false
  }
}

async function signOut() {
  conversationSyncSequence++
  pendingConversationSync = null
  questionBusy.value = false
  conversationLoadSequence++
  conversationLoading.value = false
  answerAbort?.abort()
  clearPendingAnswer()
  if (sessionState.value === 'ready') await logout()
  currentUser.value = null
  showPassword.value = false
  sessionState.value = 'login'
  docs.value = []
  editingArtifactId.value = null
  editingSourceId.value = null
  closeEditPreview()
  selectedDoc.value = null
  messages.value = []
  conversations.value = []
  activeConversationId.value = null
  conversationToDelete.value = null
  tasks.value = []
  pendingDocuments.value = []
  pendingVersions.value = []
  artifacts.value = []
  auditEvents.value = []
  gptAvailable.value = false
  collaborationAvailable.value = false
  answerMode.value = 'extract'
  draftArtifact.value = null
  slideArtifact.value = null
  collaborationRuns.value = {}
  mobileNavOpen.value = false
}

function enterDemo() {
  currentUser.value = null
  sessionState.value = 'demo'
  docs.value = [...initialDocuments]
  tasks.value = [...initialTasks]
  docSources.value = [1, 2]
  slideSources.value = [3, 4]
  messages.value = [
    { role: 'user', text: '出差报销需要哪些审批？' },
    { role: 'assistant', ...answerQuestion('出差报销需要哪些审批？', initialDocuments) }
  ]
}

type NavItem = { id: View; label: string; icon: typeof MessageSquareText }
const navMain: NavItem[] = [
  { id: 'assistant', label: '智能问答', icon: MessageSquareText },
  { id: 'library', label: '知识库', icon: BookOpenText },
  { id: 'documents', label: '部门会议室', icon: Users },
  { id: 'slides', label: '演示文稿', icon: Presentation }
]
const navManage: NavItem[] = [
  { id: 'tasks', label: '我的任务', icon: LayoutList },
  { id: 'access', label: '权限概览', icon: ShieldCheck }
]
const labels: Record<View, string> = { assistant: '智能问答', library: '知识库', documents: '部门会议室', doc_editor: '文档草稿', slides: '演示文稿', tasks: '我的任务', access: '权限概览' }
const initialHash = location.hash.slice(1) as View
const currentView = ref<View>(initialHash in labels ? initialHash : 'assistant')
const mobileNavOpen = ref(false)
const chatHistoryOpen = ref(true)
const sidebarWidth = ref(238)
const sidebarResizing = ref(false)
const sidebarStorageKey = 'enterprise-agent-sidebar-width'
function clampSidebarWidth(width: number) {
  return Math.max(190, Math.min(width, 420, window.innerWidth - 500))
}
try {
  const savedWidth = Number(localStorage.getItem(sidebarStorageKey))
  if (savedWidth >= 190 && savedWidth <= 420) sidebarWidth.value = savedWidth
} catch { /* Local storage may be unavailable in private browsing. */ }
function setSidebarWidth(width: number) {
  sidebarWidth.value = clampSidebarWidth(width)
}
function persistSidebarWidth() {
  try { localStorage.setItem(sidebarStorageKey, String(sidebarWidth.value)) } catch { /* Keep the current width. */ }
}
function stopSidebarResize() {
  if (!sidebarResizing.value) return
  sidebarResizing.value = false
  window.removeEventListener('pointermove', moveSidebarResize)
  window.removeEventListener('pointerup', stopSidebarResize)
  window.removeEventListener('pointercancel', stopSidebarResize)
  persistSidebarWidth()
}
function moveSidebarResize(event: PointerEvent) {
  if (sidebarResizing.value) setSidebarWidth(event.clientX)
}
function startSidebarResize(event: PointerEvent) {
  if (event.button !== 0) return
  event.preventDefault()
  sidebarResizing.value = true
  window.addEventListener('pointermove', moveSidebarResize)
  window.addEventListener('pointerup', stopSidebarResize)
  window.addEventListener('pointercancel', stopSidebarResize)
}
function resizeSidebarByKey(event: KeyboardEvent) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  setSidebarWidth(event.key === 'Home' ? 190 : event.key === 'End' ? 420 : sidebarWidth.value + (event.key === 'ArrowRight' ? 20 : -20))
  persistSidebarWidth()
}
onUnmounted(stopSidebarResize)
const docs = ref<LibraryDocument[]>([])
const documentsBusy = ref(false)
const documentsError = ref('')
const tasks = ref<WorkTask[]>([])
const pendingDocuments = ref<PendingDocument[]>([])
const artifacts = ref<Artifact[]>([])
const artifactPage = ref(1)
const artifactPageSize = 6
type ArtifactStatusFilter = '全部状态' | '待确认' | '已完成' | '来源异常'
const artifactStatusOptions: ArtifactStatusFilter[] = ['全部状态', '待确认', '已完成', '来源异常']
const artifactSearch = ref('')
const artifactStatusFilter = ref<ArtifactStatusFilter>('全部状态')
function artifactStatus(item: Artifact): Exclude<ArtifactStatusFilter, '全部状态'> {
  if (item.source_status !== 'current') return '来源异常'
  return item.status === 'completed' ? '已完成' : '待确认'
}
const filteredArtifacts = computed(() => {
  const query = artifactSearch.value.trim().normalize('NFKC').toLocaleLowerCase()
  return artifacts.value.filter(item =>
    (artifactStatusFilter.value === '全部状态' || artifactStatus(item) === artifactStatusFilter.value)
    && `${item.title} ${item.goal} ${item.kind}`.normalize('NFKC').toLocaleLowerCase().includes(query))
})
const pagedArtifacts = computed(() => filteredArtifacts.value.slice(
  (artifactPage.value - 1) * artifactPageSize, artifactPage.value * artifactPageSize))
watch([artifactSearch, artifactStatusFilter], () => { artifactPage.value = 1 })
watch(artifacts, (items, previous) => {
  if (items[0]?.id !== previous[0]?.id) artifactPage.value = 1
})
watch(filteredArtifacts, items => {
  artifactPage.value = Math.min(artifactPage.value, Math.max(1, Math.ceil(items.length / artifactPageSize)))
})
const editingArtifactId = ref<string | null>(null)
const editingSourceId = ref<string | null>(null)
const editingArtifact = computed(() => artifacts.value.find(item => item.id === editingArtifactId.value) ?? null)
const editingSource = computed(() => docs.value.find(item => String(item.id) === editingSourceId.value) ?? null)
const editableArtifacts = computed(() => artifacts.value.filter(item => item.status === 'pending' && item.source_status === 'current' && item.mode === 'standard'))
const editMenu = ref<HTMLDetailsElement | null>(null)
const fileSearchInput = ref<HTMLInputElement | null>(null)
const fileSearch = ref('')
const normalizedFileSearch = computed(() => fileSearch.value.trim().normalize('NFKC').toLocaleLowerCase())
const filteredEditableArtifacts = computed(() => editableArtifacts.value.filter(item =>
  `${item.title} ${item.goal} ${item.kind}`.normalize('NFKC').toLocaleLowerCase().includes(normalizedFileSearch.value)))
const filteredSourceDocs = computed(() => docs.value.filter(item =>
  `${item.title} ${item.type} ${item.area} ${item.owner} ${item.version}`.normalize('NFKC').toLocaleLowerCase().includes(normalizedFileSearch.value)))
type EditPreviewTarget = { kind: 'artifact' | 'source'; id: string; title: string }
const editPreviewTarget = ref<EditPreviewTarget | null>(null)
const editPreviewArtifact = ref<Artifact | null>(null)
const editPreviewDocument = ref<DocumentPreview | null>(null)
const editPreviewBusy = ref(false)
const editPreviewError = ref('')
let editPreviewSequence = 0
const editPreviewSelected = computed(() => editPreviewTarget.value?.kind === 'artifact'
  ? editingArtifactId.value === editPreviewTarget.value.id
  : !!editPreviewTarget.value && editingSourceId.value === editPreviewTarget.value.id)
function closeEditPreview() {
  editPreviewSequence++
  editPreviewTarget.value = null
  editPreviewArtifact.value = null
  editPreviewDocument.value = null
  editPreviewBusy.value = false
  editPreviewError.value = ''
}
async function openEditPreview(kind: EditPreviewTarget['kind'], id: string, title: string) {
  const sequence = ++editPreviewSequence
  editPreviewTarget.value = { kind, id, title }
  editPreviewArtifact.value = null
  editPreviewDocument.value = null
  editPreviewError.value = ''
  editPreviewBusy.value = true
  if (editMenu.value) editMenu.value.open = false
  try {
    if (kind === 'artifact') {
      const item = await getArtifact(id)
      if (sequence !== editPreviewSequence) return
      if (item.status !== 'pending' || item.source_status !== 'current') throw new Error('草稿已变化，请刷新任务列表')
      editPreviewArtifact.value = item
    } else {
      const item = await getDocumentPreview(id)
      if (sequence !== editPreviewSequence) return
      editPreviewDocument.value = item
    }
  } catch (error) {
    if (sequence === editPreviewSequence) editPreviewError.value = error instanceof Error ? error.message : '文件预览失败'
  } finally {
    if (sequence === editPreviewSequence) editPreviewBusy.value = false
  }
}
function choosePreviewedFile() {
  const target = editPreviewTarget.value
  if (!target || editPreviewBusy.value || editPreviewError.value) return
  if (target.kind === 'artifact') selectEditArtifact(target.id)
  else selectEditSource(target.id)
  closeEditPreview()
}
function onEditMenuToggle(event: Event) {
  if ((event.currentTarget as HTMLDetailsElement).open) requestAnimationFrame(() => fileSearchInput.value?.focus())
  else fileSearch.value = ''
}
function clearEditTarget() {
  editingArtifactId.value = null
  editingSourceId.value = null
  if (editMenu.value) editMenu.value.open = false
}
function selectEditArtifact(id: string) {
  if (editingArtifactId.value === id) clearEditTarget()
  else {
    editingArtifactId.value = id
    editingSourceId.value = null
    if (editMenu.value) editMenu.value.open = false
  }
}
function selectEditSource(id: string) {
  if (editingSourceId.value === id) clearEditTarget()
  else {
    editingSourceId.value = id
    editingArtifactId.value = null
    if (editMenu.value) editMenu.value.open = false
  }
}
const pendingVersions = ref<PendingVersion[]>([])
const auditEvents = ref<AuditEvent[]>([])
const auditBusy = ref(false)
const auditError = ref('')
const conversations = ref<ConversationSummary[]>([])
const activeConversationId = ref<string | null>(null)
const conversationLoading = ref(false)
let conversationLoadSequence = 0
let conversationListSequence = 0
const conversationToDelete = ref<ConversationSummary | null>(null)
const conversationDeleteBusy = ref(false)
const draftArtifact = ref<Artifact | null>(null)
const slideArtifact = ref<Artifact | null>(null)
const collaborationRuns = ref<Record<string, CollaborationRun>>({})
const activeCollaborationId = ref<string | null>(null)
const workBusy = ref(false)
const questionBusy = ref(false)
const gptAvailable = ref(false)
const collaborationAvailable = ref(false)
const onlineSlideImagesAvailable = ref(false)
const reflectionAvailable = ref(false)
const answerMode = ref<'extract' | 'gpt'>('extract')
const toast = ref('')
let toastTimer: ReturnType<typeof setTimeout> | undefined
const selectedDoc = ref<LibraryDocument | null>(null)
const originalBusy = ref(false)
const versionFile = ref<File | null>(null)
const versionBusy = ref(false)
const uploadOpen = ref(false)
const selectedFile = ref<File | null>(null)
const uploadName = ref('')
const uploadArea = ref<LibraryDocument['area']>('制度规范')
const uploadVisibility = ref<LibraryDocument['visibility']>('内部公开')
const search = ref('')
const categoryOptions = ['全部分类', '制度规范', '产品资料', '项目空间'] as const
const visibilityOptions = ['全部权限', '内部公开', '部门受限', '机密'] as const
const category = ref<(typeof categoryOptions)[number]>('全部分类')
const visibility = ref<(typeof visibilityOptions)[number]>('全部权限')

async function refreshDocuments() {
  if (sessionState.value !== 'ready') return
  const tokenAtStart = savedToken()
  documentsBusy.value = true
  documentsError.value = ''
  try {
    const visibleDocuments = await listDocuments()
    if (sessionState.value !== 'ready' || tokenAtStart !== savedToken()) return
    docs.value = visibleDocuments
    const visibleIds = new Set(docs.value.map(document => document.id))
    if (selectedDoc.value && !visibleIds.has(selectedDoc.value.id)) selectedDoc.value = null
    if (editingSourceId.value && !docs.value.some(doc => String(doc.id) === editingSourceId.value)) editingSourceId.value = null
    docSources.value = docSources.value.filter(id => visibleIds.has(id))
    slideSources.value = slideSources.value.filter(id => visibleIds.has(id))
  } catch (error) {
    if (sessionState.value !== 'ready' || tokenAtStart !== savedToken()) return
    docs.value = []
    documentsError.value = error instanceof Error ? error.message : '资料加载失败'
    if (!savedToken()) sessionState.value = 'login'
  } finally {
    documentsBusy.value = false
  }
}

async function refreshWorkspace() {
  if (sessionState.value !== 'ready') return
  const tokenAtStart = savedToken()
  try {
    const [pending, versions, saved] = await Promise.all([listPendingDocuments(), listPendingVersions(), listArtifacts()])
    if (sessionState.value !== 'ready' || tokenAtStart !== savedToken()) return
    pendingDocuments.value = pending
    pendingVersions.value = versions
    artifacts.value = saved
    if (editingArtifactId.value && !editableArtifacts.value.some(item => item.id === editingArtifactId.value)) editingArtifactId.value = null
    tasks.value = saved.map(item => ({
      id: item.id, title: item.title, type: item.kind === 'pptx' ? '演示文稿' : '文档整理',
      sources: item.source_ids.length, updated: '已保存', status: item.status === 'completed' ? '已完成' : '待核对'
    }))
  } catch (error) {
    if (sessionState.value === 'ready' && tokenAtStart === savedToken()) {
      tasks.value = []
      pendingDocuments.value = []
      pendingVersions.value = []
      artifacts.value = []
      editingArtifactId.value = null
      notify(error instanceof Error ? error.message : '任务加载失败')
    }
  }
}

async function refreshAuditEvents() {
  if (sessionState.value !== 'ready' || currentUser.value?.role !== 'system_admin') return
  const tokenAtStart = savedToken()
  auditBusy.value = true
  auditError.value = ''
  try {
    const events = await listAuditEvents()
    if (sessionState.value === 'ready' && tokenAtStart === savedToken()) auditEvents.value = events
  } catch (error) {
    if (sessionState.value === 'ready' && tokenAtStart === savedToken()) {
      auditEvents.value = []
      auditError.value = error instanceof Error ? error.message : '操作记录加载失败'
    }
  } finally {
    auditBusy.value = false
  }
}

const auditActionLabels: Record<string, string> = {
  'auth.login': '登录', 'auth.logout': '退出登录',
  'document.upload': '上传资料', 'document.publish': '发布资料',
  'document.original.download': '下载资料原件',
  'document.deny': '拒绝访问', 'document.allow': '恢复访问',
  'artifact.create': '创建文件任务', 'artifact.confirm': '确认文件任务',
  'artifact.download': '下载文件'
}

async function refreshCapabilities() {
  if (sessionState.value !== 'ready') return
  const tokenAtStart = savedToken()
  try {
    const capabilities = await getCapabilities()
    if (sessionState.value === 'ready' && tokenAtStart === savedToken()) {
      gptAvailable.value = capabilities.gpt_quotes
      collaborationAvailable.value = capabilities.collaboration
      onlineSlideImagesAvailable.value = capabilities.online_slide_images
      reflectionAvailable.value = capabilities.reflection
      if (!capabilities.gpt_quotes) answerMode.value = 'extract'
    }
  } catch {
    if (sessionState.value === 'ready' && tokenAtStart === savedToken()) {
      gptAvailable.value = false
      collaborationAvailable.value = false
      onlineSlideImagesAvailable.value = false
      reflectionAvailable.value = false
      answerMode.value = 'extract'
    }
  }
}

function resumeArtifact(item: Artifact, loadRun = true) {
  preparedQuestion.value = null
  if (item.source_status === 'stale') {
    notify('来源资料版本已变化，请重新创建任务')
    return
  }
  if (item.source_status === 'unverified') notify('此任务创建时未记录来源版本，请核对资料后使用')
  if (loadRun) void refreshCollaborationRun(item)
  if (item.kind === 'docx') {
    draftArtifact.value = item
    docMode.value = item.mode === 'comparison' ? '对比' : '摘要'
    syncReviewEdits(item)
    syncNoteEdits(item)
    draftTitle.value = item.title
    draftGoal.value = item.goal
    docSources.value = [...item.source_ids]
    if (item.mode === 'comparison') comparisonTopics.value = item.outline.join('\n')
    outline.value = [...item.outline]
    draftReady.value = true
    navigate('doc_editor')
  } else {
    slideArtifact.value = item
    syncNoteEdits(item)
    slideTitle.value = item.title
    slideAudience.value = item.goal
    slideTheme.value = item.theme
    slideSources.value = [...item.source_ids]
    slideOutline.value = [...item.outline]
    slideCount.value = item.outline.length
    slideIndex.value = 0
    slideReady.value = true
    navigate('slides')
  }
}

async function refreshCollaborationRun(item: Artifact) {
  if (sessionState.value !== 'ready' || item.source_status !== 'current') return
  const tokenAtStart = savedToken()
  try {
    const run = await getCollaborationRun(item.id)
    if (tokenAtStart === savedToken()) collaborationRuns.value[item.id] = run
  } catch {
    if (tokenAtStart === savedToken()) delete collaborationRuns.value[item.id]
  }
}

function collaborationRetryExhausted(item: Artifact | null): boolean {
  if (!item) return false
  const run = collaborationRuns.value[item.id]
  return run?.status === 'failed' && run.draft_token === item.draft_token && !run.can_retry
}

function openTask(task: WorkTask) {
  const saved = artifacts.value.find(item => item.id === task.id)
  if (sessionState.value === 'ready' && saved) resumeArtifact(saved)
  else navigate(task.type === '演示文稿' ? 'slides' : 'doc_editor')
}

async function approveDocument(id: string | number) {
  try {
    await publishDocument(id)
    await Promise.all([refreshDocuments(), refreshWorkspace()])
    notify('资料已发布')
  } catch (error) { notify(error instanceof Error ? error.message : '发布失败') }
}

async function submitVersion() {
  if (!selectedDoc.value || !versionFile.value || versionBusy.value) return
  versionBusy.value = true
  try {
    await uploadDocumentVersion(selectedDoc.value.id, versionFile.value)
    versionFile.value = null
    selectedDoc.value = null
    await refreshWorkspace()
    notify('新版本已提交审核，现有资料仍可使用')
  } catch (error) { notify(error instanceof Error ? error.message : '提交新版本失败') }
  finally { versionBusy.value = false }
}

async function approveVersion(id: string) {
  try {
    await publishDocumentVersion(id)
    await Promise.all([refreshDocuments(), refreshWorkspace()])
    notify('新版本已发布，旧任务需核对来源')
  } catch (error) { notify(error instanceof Error ? error.message : '发布新版本失败') }
}

async function downloadOriginal() {
  if (!selectedDoc.value?.has_original || originalBusy.value) return
  originalBusy.value = true
  try {
    await downloadDocumentOriginal(selectedDoc.value.id, selectedDoc.value.version)
    notify('原件已下载')
  } catch (error) {
    notify(error instanceof Error ? error.message : '原件下载失败')
    selectedDoc.value = null
    void refreshDocuments()
  } finally { originalBusy.value = false }
}

watch(selectedDoc, async (document) => {
  if (!document || sessionState.value !== 'ready' || document.excerpt) return
  try {
    const detail = await getDocument(document.id)
    if (selectedDoc.value?.id === document.id) selectedDoc.value = detail
  } catch (error) {
    selectedDoc.value = null
    notify(error instanceof Error ? error.message : '资料不可访问')
    void refreshDocuments()
  }
})


function notify(message: string) {
  toast.value = message
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.value = '' }, 3500)
}
function navigate(view: View) {
  if (view === 'assistant') chatHistoryOpen.value = true
  currentView.value = view
  history.replaceState(null, '', `#${view}`)
  mobileNavOpen.value = false
  if (view === 'library') void refreshDocuments()
  if (view === 'access') void refreshAuditEvents()
}
function selectMainNav(view: View) {
  if (view === 'assistant' && currentView.value === 'assistant') chatHistoryOpen.value = !chatHistoryOpen.value
  else navigate(view)
}
window.addEventListener('hashchange', () => {
  const hash = location.hash.slice(1) as View
  if (hash in labels) currentView.value = hash
})

const filteredDocs = computed(() => docs.value.filter(doc =>
  (category.value === '全部分类' || doc.area === category.value) &&
  (visibility.value === '全部权限' || doc.visibility === visibility.value) &&
  `${doc.title} ${doc.owner} ${doc.excerpt}`.toLowerCase().includes(search.value.trim().toLowerCase())
))
const libraryPage = ref(1)
const libraryPageSize = 10
const pagedDocs = computed(() => filteredDocs.value.slice(
  (libraryPage.value - 1) * libraryPageSize, libraryPage.value * libraryPageSize))
watch([search, category, visibility], () => { libraryPage.value = 1 })
watch(filteredDocs, items => {
  libraryPage.value = Math.min(libraryPage.value, Math.max(1, Math.ceil(items.length / libraryPageSize)))
})
const scopedDocs = computed(() => docs.value)
const categoryMenu = ref<HTMLDetailsElement | null>(null)
const visibilityMenu = ref<HTMLDetailsElement | null>(null)
function selectCategory(value: typeof category.value) {
  category.value = value
  if (categoryMenu.value) {
    categoryMenu.value.open = false
    categoryMenu.value.querySelector('summary')?.focus()
  }
}
function selectVisibility(value: typeof visibility.value) {
  visibility.value = value
  if (visibilityMenu.value) {
    visibilityMenu.value.open = false
    visibilityMenu.value.querySelector('summary')?.focus()
  }
}
function closeScopeMenu(event: PointerEvent) {
  if (editMenu.value && !editMenu.value.contains(event.target as Node)) editMenu.value.open = false
  if (categoryMenu.value && !categoryMenu.value.contains(event.target as Node)) categoryMenu.value.open = false
  if (visibilityMenu.value && !visibilityMenu.value.contains(event.target as Node)) visibilityMenu.value.open = false
}
onMounted(() => document.addEventListener('pointerdown', closeScopeMenu))
onUnmounted(() => document.removeEventListener('pointerdown', closeScopeMenu))
type RetryRequest = { question: string; area: string | null; mode: 'extract' | 'gpt'; conversationId: string | null; workflowRunId: string | null; target?: { artifact_id: string } | { source_document_id: string } }
type Message = { role: 'user' | 'assistant'; text?: string; paragraphs?: string[]; citations?: LibraryDocument[]; edit?: EditProposal | null; clarification?: Clarification | null; answerStatus?: string; documentRequested?: boolean; draftError?: string; mode?: 'extract' | 'gpt' | 'general' | 'file_access' | 'artifact_edit' | 'file_answer' | 'demo'; error?: boolean; streaming?: boolean; retry?: RetryRequest }
const messages = ref<Message[]>([])
const composingAnswerKey = ref<string | null>(null)
const clarificationIndex = ref<number | null>(null)
const clarificationValue = ref('')
const activeClarification = computed(() => clarificationIndex.value === null ? null : messages.value[clarificationIndex.value]?.clarification ?? null)
function openClarification(index: number) { clarificationValue.value = ''; clarificationIndex.value = index }
function closeClarification() { clarificationIndex.value = null; clarificationValue.value = '' }
async function submitClarification() {
  const clarification = activeClarification.value
  const value = clarificationValue.value.trim()
  if (!clarification || !value || questionBusy.value || conversationLoading.value) return
  if (clarification.input_type === 'number' && (!Number.isFinite(Number(value)) || Number(value) < 0 || Number(value) > 100)) return
  if (clarification.input_type === 'select' && !clarification.options.includes(value)) return
  closeClarification()
  const original = clarification.original_question.slice(0, 900)
  await ask(`${original}（我的${clarification.field}是${value}${clarification.unit}）`)
}
const messageList = ref<HTMLElement | null>(null)
let messageScrollFrame = 0
function followLatestMessage() {
  if (messageScrollFrame) return
  messageScrollFrame = requestAnimationFrame(() => {
    messageScrollFrame = 0
    if (messageList.value) messageList.value.scrollTop = messageList.value.scrollHeight
  })
}
watch(messages, followLatestMessage, { deep: true, flush: 'post' })
onUnmounted(() => { if (messageScrollFrame) cancelAnimationFrame(messageScrollFrame) })
const question = ref('')
let answerAbort: AbortController | null = null
function conversationStorageKey(): string | null {
  return currentUser.value ? `enterprise-agent-active:${currentUser.value.org_id}:${currentUser.value.user_id}` : null
}
function pendingAnswerKey(): string | null {
  return currentUser.value ? `enterprise-agent-pending:${currentUser.value.org_id}:${currentUser.value.user_id}` : null
}
function savePendingAnswer(retry: RetryRequest) {
  const key = pendingAnswerKey()
  if (key && retry.workflowRunId) sessionStorage.setItem(key, JSON.stringify(retry))
}
function clearPendingAnswer(runId?: string | null) {
  const key = pendingAnswerKey()
  if (!key) return
  if (!runId || readPendingAnswer()?.workflowRunId === runId) sessionStorage.removeItem(key)
}
function readPendingAnswer(): RetryRequest | null {
  const key = pendingAnswerKey()
  if (!key) return null
  try {
    const value = JSON.parse(sessionStorage.getItem(key) ?? 'null') as Partial<RetryRequest> | null
    if (!value || value.mode !== 'extract' || typeof value.question !== 'string' || !value.question.trim() ||
        typeof value.workflowRunId !== 'string' || !value.workflowRunId ||
        (value.area !== null && typeof value.area !== 'string') ||
        (value.conversationId !== null && typeof value.conversationId !== 'string')) return null
    return value as RetryRequest
  } catch { return null }
}
function citationDocuments(citations: Citation[]): LibraryDocument[] {
  return citations.map(citation => {
    const document = docs.value.find(doc => String(doc.id) === citation.document_id)
    return document ? { ...document, page: citation.location, excerpt: citation.excerpt ?? document.excerpt } : null
  }).filter((doc): doc is LibraryDocument => doc !== null)
}
async function refreshConversations() {
  if (sessionState.value !== 'ready') return
  const tokenAtStart = savedToken()
  const listSequence = ++conversationListSequence
  try {
    const items = await listConversations()
    if (sessionState.value === 'ready' && tokenAtStart === savedToken() && listSequence === conversationListSequence)
      conversations.value = items
  } catch {
    if (sessionState.value === 'ready' && tokenAtStart === savedToken() && listSequence === conversationListSequence)
      notify('会话列表加载失败')
  }
}
async function restoreActiveConversation() {
  const tokenAtStart = savedToken()
  const userAtStart = currentUser.value?.user_id
  try {
    const items = await listConversations()
    if (sessionState.value !== 'ready' || tokenAtStart !== savedToken() || userAtStart !== currentUser.value?.user_id) return
    conversations.value = items
    const key = conversationStorageKey()
    const saved = key ? sessionStorage.getItem(key) : null
    if (saved !== 'new') {
      const target = items.find(item => item.id === saved) ?? items[0]
      if (target) await openConversation(target.id, true)
    }
    await restorePendingAnswer(tokenAtStart, userAtStart)
  } catch {
    if (sessionState.value === 'ready') notify('会话恢复失败')
  }
}
async function restorePendingAnswer(tokenAtStart: string | null, userAtStart: string | undefined) {
  const retry = readPendingAnswer()
  if (!retry?.workflowRunId) return
  try {
    const result = await getWorkflowConversation(retry.workflowRunId)
    if (sessionState.value !== 'ready' || tokenAtStart !== savedToken() || userAtStart !== currentUser.value?.user_id) return
    if (result.conversation_id) {
      if (await openConversation(result.conversation_id, true)) clearPendingAnswer(retry.workflowRunId)
    } else if (retry.conversationId === activeConversationId.value) {
      messages.value.push({ role: 'user', text: retry.question }, {
        role: 'assistant', mode: retry.mode, error: true, retry,
        paragraphs: ['回答中断，请重试'], citations: [] })
    } else {
      clearPendingAnswer(retry.workflowRunId)
    }
  } catch {
    if (sessionState.value === 'ready') notify('未完成回答状态暂不可查，请刷新重试')
  }
}
async function openConversation(id: string, preservePending = false): Promise<boolean> {
  if (sessionState.value !== 'ready' || questionBusy.value) return false
  conversationSyncSequence++
  const tokenAtStart = savedToken()
  const loadSequence = ++conversationLoadSequence
  conversationLoading.value = true
  try {
    const [detail, visibleDocuments] = await Promise.all([getConversation(id), listDocuments()])
    if (sessionState.value !== 'ready' || tokenAtStart !== savedToken() || loadSequence !== conversationLoadSequence) return false
    if (!preservePending) clearPendingAnswer()
    docs.value = visibleDocuments
    activeConversationId.value = id
    const key = conversationStorageKey()
    if (key) sessionStorage.setItem(key, id)
    messages.value = detail.turns.flatMap(turn => [
      { role: 'user' as const, text: turn.question },
      { role: 'assistant' as const, mode: turn.mode, answerStatus: turn.answer.status, paragraphs: turn.answer.paragraphs,
        citations: citationDocuments(turn.answer.citations), edit: turn.answer.edit, clarification: turn.answer.clarification,
        documentRequested: turn.answer.document_requested }
    ])
    mobileNavOpen.value = false
    navigate('assistant')
    return true
  } catch (error) {
    if (loadSequence === conversationLoadSequence && sessionState.value === 'ready')
      notify(error instanceof Error ? error.message : '会话加载失败')
    return false
  } finally {
    if (loadSequence === conversationLoadSequence) conversationLoading.value = false
  }
}
async function syncActiveConversation(id: string) {
  if (sessionState.value !== 'ready' || questionBusy.value || conversationLoading.value || activeConversationId.value !== id) return
  const scope = conversationScope()
  const token = savedToken()
  const sequence = ++conversationSyncSequence
  try {
    const detail = await getConversation(id)
    if (scope !== conversationScope() || token !== savedToken() || sequence !== conversationSyncSequence ||
        sessionState.value !== 'ready' || questionBusy.value || activeConversationId.value !== id) return
    messages.value = detail.turns.flatMap(turn => [
      { role: 'user' as const, text: turn.question },
      { role: 'assistant' as const, mode: turn.mode, answerStatus: turn.answer.status, paragraphs: turn.answer.paragraphs,
        citations: citationDocuments(turn.answer.citations), edit: turn.answer.edit, clarification: turn.answer.clarification,
        documentRequested: turn.answer.document_requested }
    ])
  } catch {
    // A concurrent delete is resolved by the next focus refresh or delete event.
  }
}
async function confirmDeleteConversation() {
  const target = conversationToDelete.value
  if (!target || conversationDeleteBusy.value || questionBusy.value || conversationLoading.value) return
  const tokenAtStart = savedToken()
  conversationDeleteBusy.value = true
  try {
    await deleteConversation(target.id)
    if (sessionState.value !== 'ready' || tokenAtStart !== savedToken()) return
    conversationListSequence++
    conversations.value = conversations.value.filter(item => item.id !== target.id)
    if (readPendingAnswer()?.conversationId === target.id) clearPendingAnswer()
    if (activeConversationId.value === target.id) newChat()
    publishConversationSync('delete', target.id)
    conversationToDelete.value = null
    notify('会话已删除')
    void refreshConversations()
  } catch (error) {
    if (sessionState.value === 'ready') notify(error instanceof Error ? error.message : '删除会话失败')
  } finally { conversationDeleteBusy.value = false }
}
async function ask(value = question.value) {
  const trimmed = value.trim()
  if (!trimmed || questionBusy.value || conversationLoading.value) return
  messages.value.push({ role: 'user', text: trimmed })
  question.value = ''
  if (sessionState.value === 'demo') {
    messages.value.push({ role: 'assistant', mode: 'demo', ...answerQuestion(trimmed, scopedDocs.value) })
    return
  }
  clearPendingAnswer()
  const target = editingArtifactId.value ? { artifact_id: editingArtifactId.value } :
    editingSourceId.value ? { source_document_id: editingSourceId.value } : undefined
  const retry: RetryRequest = { question: trimmed, area: null,
    mode: answerMode.value, conversationId: activeConversationId.value, workflowRunId: null, target }
  const replyIndex = messages.value.length
  messages.value.push({ role: 'assistant', mode: retry.mode, paragraphs: [''], citations: [], streaming: true, retry })
  await sendAnswer(replyIndex, retry)
}
async function applyChatEdit(index: number, proposal: EditProposal) {
  if (questionBusy.value || proposal.status !== 'pending') return
  questionBusy.value = true
  try {
    await confirmArtifactEdit(proposal.id)
    if (messages.value[index]?.edit?.id === proposal.id) messages.value[index].edit = { ...proposal, status: 'applied' }
    await refreshWorkspace()
    notify('修改已保存到待确认草稿')
  } catch (error) { notify(error instanceof Error ? error.message : '保存修改失败') }
  finally { questionBusy.value = false }
}
async function retryAnswer(replyIndex: number) {
  if (questionBusy.value || conversationLoading.value) return
  const retry = messages.value[replyIndex]?.retry
  if (!retry) return
  messages.value[replyIndex] = { role: 'assistant', mode: retry.mode, paragraphs: [''], citations: [], streaming: true, retry }
  await sendAnswer(replyIndex, retry)
}
async function sendAnswer(replyIndex: number, retry: RetryRequest) {
  questionBusy.value = true
  const tokenAtStart = savedToken()
  const controller = new AbortController()
  answerAbort = controller
  try {
    await streamConversationAnswer(retry.question, retry.area, retry.mode,
      retry.conversationId, event => {
        if (answerAbort !== controller || sessionState.value !== 'ready' || tokenAtStart !== savedToken()) return
        if (event.type === 'meta') {
          retry.workflowRunId = event.workflow_run_id
          if (retry.workflowRunId) savePendingAnswer(retry)
          messages.value[replyIndex] = { ...messages.value[replyIndex], mode: event.intent === 'general' ? 'general' : event.intent === 'file_access' ? 'file_access' : event.intent === 'file_answer' ? 'file_answer' : event.intent === 'edit' ? 'artifact_edit' : retry.mode, retry: { ...retry } }
        } else if (event.type === 'delta') {
          const previous = messages.value[replyIndex]
          messages.value[replyIndex] = { ...previous, paragraphs: [(previous.paragraphs?.[0] ?? '') + event.text] }
        } else if (event.type === 'done') {
          clearPendingAnswer(retry.workflowRunId)
          activeConversationId.value = event.conversation_id
          const key = conversationStorageKey()
          if (key) sessionStorage.setItem(key, event.conversation_id)
          messages.value[replyIndex] = { role: 'assistant', mode: event.intent === 'general' ? 'general' : event.intent === 'file_access' ? 'file_access' : event.intent === 'file_answer' ? 'file_answer' : event.intent === 'edit' ? 'artifact_edit' : retry.mode, answerStatus: event.answer.status,
            paragraphs: event.answer.paragraphs, citations: citationDocuments(event.answer.citations), edit: event.answer.edit, clarification: event.answer.clarification,
            documentRequested: event.answer.document_requested }
          if (event.answer.status === 'needs_clarification') openClarification(replyIndex)
          if (event.intent === 'edit') void refreshWorkspace()
          publishConversationSync('upsert', event.conversation_id)
        }
      }, controller.signal, retry.workflowRunId, retry.target)
    if (answerAbort !== controller || sessionState.value !== 'ready' || tokenAtStart !== savedToken()) return
    void refreshConversations()
  } catch (error) {
    if (answerAbort !== controller || sessionState.value !== 'ready' || tokenAtStart !== savedToken()) return
    const needsConfiguration = error instanceof AnswerStreamError &&
      (error.code === 'MODEL_AUTH_FAILED' || error.code === 'GENERAL_CHAT_NOT_CONFIGURED')
    const mode = error instanceof AnswerStreamError && error.code === 'GENERAL_CHAT_NOT_CONFIGURED'
      ? 'general' : messages.value[replyIndex]?.mode ?? retry.mode
    messages.value[replyIndex] = { role: 'assistant', mode, error: true, retry: needsConfiguration ? undefined : { ...retry },
      paragraphs: [error instanceof Error ? error.message : '问答失败'], citations: [] }
  }
  finally {
    if (answerAbort !== controller) return
    answerAbort = null
    questionBusy.value = false
    if (pendingConversationSync === activeConversationId.value && pendingConversationSync) {
      const id = pendingConversationSync
      pendingConversationSync = null
      void syncActiveConversation(id)
    }
  }
}
function newChat() { if (questionBusy.value) return; closeClarification(); conversationSyncSequence++; pendingConversationSync = null; clearPendingAnswer(); conversationLoadSequence++; conversationLoading.value = false; activeConversationId.value = null; messages.value = []; question.value = ''; editingArtifactId.value = null; editingSourceId.value = null; const key = conversationStorageKey(); if (key) sessionStorage.setItem(key, 'new') }
function askAbout(doc: LibraryDocument) {
  selectedDoc.value = null
  navigate('assistant')
  editingArtifactId.value = null
  editingSourceId.value = String(doc.id)
  void ask(`请概括《${doc.title}》`)
}
function onQuestionKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); ask() }
}

const docModes = [
  { id: '摘要', icon: FileText, label: '资料摘要', subtitle: '按来源整理摘录' },
  { id: '对比', icon: SlidersHorizontal, label: '多资料对比', subtitle: '分来源核对差异' },
  { id: '流程', icon: LayoutList, label: '流程说明', subtitle: '梳理步骤与职责' },
  { id: '报告', icon: Files, label: '报告草稿', subtitle: '按结构组织信息' }
] as const
const docMode = ref<(typeof docModes)[number]['id']>('摘要')
const docSources = ref<(string | number)[]>([])
const docSourceSearch = ref('')
const docSourcePage = ref(1)
const preparedQuestion = ref<string | null>(null)
const draftTitle = ref('差旅报销流程说明')
const draftGoal = ref('梳理申请、审批、财务审核及到账时间，面向新员工阅读。')
const comparisonTopics = ref('适用范围\n审批流程\n时间与条件')
type ReviewEdit = { heading: string; decision: ComparisonReview['decision'] | ''; note: string }
const reviewEdits = ref<ReviewEdit[]>([])
const docNoteEdits = ref<string[]>([])
const slideNoteEdits = ref<string[]>([])
const draftReady = ref(false)
const outline = ref<string[]>([])
const draftEvidenceCurrent = computed(() => draftArtifact.value?.section_drafts.length === outline.value.length &&
  draftArtifact.value.section_drafts.every((section, index) => section.heading === outline.value[index]))
const docNotesDirty = computed(() => !!draftArtifact.value && draftEvidenceCurrent.value &&
  draftArtifact.value.section_drafts.some((section, index) => (docNoteEdits.value[index] ?? '').trim() !== (section.manual_note ?? '')))
const comparisonReviewSaved = computed(() => draftArtifact.value?.mode === 'comparison' && draftEvidenceCurrent.value &&
  draftArtifact.value.section_drafts.every((section, index) => !!section.review &&
    section.heading === reviewEdits.value[index]?.heading &&
    section.review.decision === reviewEdits.value[index]?.decision &&
    section.review.note === reviewEdits.value[index]?.note.trim()))
const draftInputsChanged = computed(() => !!draftArtifact.value && (
  draftArtifact.value.title !== draftTitle.value || draftArtifact.value.goal !== draftGoal.value ||
  draftArtifact.value.mode !== (docMode.value === '对比' ? 'comparison' : 'standard') ||
  (draftArtifact.value.mode === 'comparison' && draftArtifact.value.outline.join('\n') !== comparisonTopics.value.trim()) ||
  [...draftArtifact.value.source_ids].sort().join('|') !== docSources.value.map(String).sort().join('|')))
const slideSources = ref<(string | number)[]>([])
const slideSourceSearch = ref('')
const slideSourcePage = ref(1)
const sourcePageSize = 6
function sourceMatches(doc: LibraryDocument, query: string): boolean {
  return `${doc.title} ${doc.area} ${doc.owner}`.toLowerCase().includes(query.trim().toLowerCase())
}
const docSourceChoices = computed(() => docs.value.filter(doc => sourceMatches(doc, docSourceSearch.value)))
const slideSourceChoices = computed(() => docs.value.filter(doc => sourceMatches(doc, slideSourceSearch.value)))
const pagedDocSourceChoices = computed(() => docSourceChoices.value.slice(
  (docSourcePage.value - 1) * sourcePageSize, docSourcePage.value * sourcePageSize))
const pagedSlideSourceChoices = computed(() => slideSourceChoices.value.slice(
  (slideSourcePage.value - 1) * sourcePageSize, slideSourcePage.value * sourcePageSize))
watch(docSourceSearch, () => { docSourcePage.value = 1 })
watch(slideSourceSearch, () => { slideSourcePage.value = 1 })
watch(docSourceChoices, items => {
  docSourcePage.value = Math.min(docSourcePage.value, Math.max(1, Math.ceil(items.length / sourcePageSize)))
})
watch(slideSourceChoices, items => {
  slideSourcePage.value = Math.min(slideSourcePage.value, Math.max(1, Math.ceil(items.length / sourcePageSize)))
})
const slideTitle = ref('产品季度进展汇报')
const slideAudience = ref('部门例会')
const slideCount = ref(6)
const slideTheme = ref<Artifact['theme']>('brief')
const slideReady = ref(false)
const slideIndex = ref(0)
const slideOutline = ref<string[]>(['产品季度进展汇报', '本季度工作概览', '关键能力进展', '客户反馈', '风险与应对', '下阶段计划'])
const activeSlideTitle = computed(() => slideOutline.value[slideIndex.value] ?? '演示文稿')
const activeSlideDraft = computed(() => slideArtifact.value?.section_drafts[slideIndex.value])
const slideEvidenceCurrent = computed(() => slideArtifact.value?.section_drafts.length === slideOutline.value.length &&
  slideArtifact.value.section_drafts.every((section, index) => section.heading === slideOutline.value[index]))
const slideNotesDirty = computed(() => !!slideArtifact.value && slideEvidenceCurrent.value &&
  slideArtifact.value.section_drafts.some((section, index) => (slideNoteEdits.value[index] ?? '').trim() !== (section.manual_note ?? '')))
const slideInputsChanged = computed(() => !!slideArtifact.value && (
  slideArtifact.value.title !== slideTitle.value || slideArtifact.value.goal !== slideAudience.value ||
  slideArtifact.value.theme !== slideTheme.value || slideArtifact.value.outline.length !== slideCount.value ||
  [...slideArtifact.value.source_ids].sort().join('|') !== slideSources.value.map(String).sort().join('|')))
const slidePreviewTheme = computed(() => slideArtifact.value?.theme ?? slideTheme.value)
const slideBrandName = computed(() => slideArtifact.value?.brand_name ??
  (currentUser.value?.org_id === 'demo-org' ? '星图科技' : currentUser.value?.org_id ?? '星图科技'))
const slidePreviewLayout = computed(() => slideIndex.value === 0 ? 'cover' :
  slideIndex.value === slideOutline.value.length - 1 ? 'closing' : slideIndex.value % 2 ? 'split' : 'focus')
const slidePreviewReverse = computed(() => slidePreviewLayout.value === 'split' && (slideIndex.value + 1) % 4 === 0)
const slidePreviewDark = computed(() => slidePreviewLayout.value === 'closing' ||
  (slidePreviewLayout.value === 'focus' && slidePreviewTheme.value !== 'strategy') ||
  (slidePreviewTheme.value === 'strategy' && !slidePreviewReverse.value &&
    ['cover', 'split'].includes(slidePreviewLayout.value)))
const activeSlideVisual = computed(() => slideArtifact.value?.slide_visuals?.find(visual => visual.page_index === slideIndex.value))
const slideImageBlobUrl = ref('')
const slidePreviewImage = computed(() => {
  if (activeSlideVisual.value?.source !== 'local' && slideImageBlobUrl.value) return slideImageBlobUrl.value
  if (activeSlideVisual.value?.asset_name) return `/slide-assets/${activeSlideVisual.value.asset_name}`
  return slidePreviewLayout.value === 'cover' ? '/slide-assets/workspace.jpg' :
    slidePreviewLayout.value === 'closing' ? '/slide-assets/planning.jpg' :
    slideIndex.value % 4 === 1 ? '/slide-assets/team.jpg' : '/slide-assets/planning.jpg'
})
watch([slideArtifact, slideIndex], async (_value, _oldValue, onCleanup) => {
  const item = slideArtifact.value
  const visual = item?.slide_visuals?.find(entry => entry.page_index === slideIndex.value)
  const controller = new AbortController()
  let objectUrl = ''
  slideImageBlobUrl.value = ''
  onCleanup(() => {
    controller.abort()
    if (objectUrl) URL.revokeObjectURL(objectUrl)
  })
  if (!item || !visual || visual.source === 'local') return
  try {
    objectUrl = URL.createObjectURL(await getSlideVisualImage(item.id, slideIndex.value, controller.signal))
    if (!controller.signal.aborted) slideImageBlobUrl.value = objectUrl
  } catch { /* Use the local theme asset when the protected image is unavailable. */ }
}, { immediate: true })

function prepareDocumentFromAnswer(index: number) {
  if (!availableDocumentActions(index).manual) return
  const answer = messages.value[index]
  const sourceIds = [...new Set(answer?.citations?.map(citation => String(citation.id)) ?? [])].slice(0, 10)
  const prompt = messages.value[index - 1]?.text?.trim()
  if (!prompt || !sourceIds.length || sessionState.value !== 'ready') return
  docMode.value = '摘要'
  docSources.value = sourceIds
  draftTitle.value = `${prompt.slice(0, 80)} · 资料整理`
  draftGoal.value = prompt.slice(0, 1000)
  preparedQuestion.value = prompt.slice(0, 100)
  draftArtifact.value = null
  draftReady.value = false
  outline.value = []
  docNoteEdits.value = []
  navigate('doc_editor')
}

function availableDocumentActions(index: number) {
  const answer = messages.value[index]
  return documentActions(messages.value[index - 1]?.text, {
    status: answer?.answerStatus,
    paragraphs: answer?.paragraphs,
    citationCount: answer?.citations?.length ?? 0,
    documentRequested: answer?.documentRequested
  })
}

async function composeDocumentFromAnswer(index: number) {
  const prompt = messages.value[index - 1]?.text?.trim()
  if (!prompt || !availableDocumentActions(index).generate || !activeConversationId.value || workBusy.value || !collaborationAvailable.value) return
  const requestId = `answer-doc-${activeConversationId.value}-${Math.floor(index / 2)}`
  const conversationId = activeConversationId.value
  messages.value[index] = { ...messages.value[index], draftError: undefined }
  composingAnswerKey.value = requestId
  workBusy.value = true
  try {
    const result = await composeAndRunFromQuestion(prompt, requestId, {
      conversation_id: conversationId, turn_index: Math.floor(index / 2)
    })
    resumeArtifact(result.artifact, false)
    if (result.run) collaborationRuns.value[result.artifact.id] = result.run
    await refreshWorkspace()
    notify(result.status === 'completed' ? 'AI 协作草稿已生成，请核对来源和建议文字'
      : result.status === 'failed' ? '草稿已保存；请在预览中查看失败节点并继续生成'
        : '已打开此问题的文档草稿，请核对来源')
  } catch (error) {
    const reason = error instanceof Error ? error.message : '创建文档草稿失败'
    if (activeConversationId.value === conversationId && messages.value[index - 1]?.text?.trim() === prompt)
      messages.value[index] = { ...messages.value[index], draftError: reason }
    notify(reason)
  }
  finally { workBusy.value = false; composingAnswerKey.value = null }
}

function addTask(title: string, type: string, sources: number) {
  tasks.value.unshift({ id: Date.now(), title, type, sources, updated: '刚刚', status: '待核对' })
}
function syncReviewEdits(item: Artifact) {
  reviewEdits.value = item.mode === 'comparison'
    ? item.section_drafts.map(section => ({ heading: section.heading, decision: section.review?.decision ?? '', note: section.review?.note ?? '' }))
    : []
}
function syncNoteEdits(item: Artifact) {
  const notes = item.section_drafts.map(section => section.manual_note ?? '')
  if (item.kind === 'docx') docNoteEdits.value = notes
  else slideNoteEdits.value = notes
}
async function saveNotes(item: Artifact | null) {
  if (!item || item.status !== 'pending') return
  const notes = item.kind === 'docx' ? docNoteEdits.value : slideNoteEdits.value
  const headings = item.kind === 'docx' ? outline.value : slideOutline.value
  if (item.section_drafts.some((section, index) => section.heading !== headings[index]))
    return notify('请先更新章节证据')
  workBusy.value = true
  try {
    const saved = await saveArtifactNotes(item.id, item.draft_token,
      item.section_drafts.map((section, index) => ({ heading: section.heading, text: notes[index] ?? '' })))
    if (item.kind === 'docx') draftArtifact.value = saved
    else slideArtifact.value = saved
    if (saved.draft_token !== item.draft_token) delete collaborationRuns.value[item.id]
    syncNoteEdits(saved)
    notify('人工补充已保存')
  } catch (error) { notify(error instanceof Error ? error.message : '保存失败') }
  finally { workBusy.value = false }
}
async function generateDraft() {
  const sourceNames = docs.value.filter(doc => docSources.value.includes(doc.id)).map(doc => doc.title)
  if (!draftTitle.value.trim() || sourceNames.length === 0) return notify('请填写标题并选择可见来源资料')
  if (docMode.value === '对比' && sourceNames.length < 2) return notify('多资料对比至少选择两份资料')
  const topics = comparisonTopics.value.split(/\r?\n/).map(topic => topic.trim()).filter(Boolean)
  if (docMode.value === '对比' && (topics.length < 1 || topics.length > 10 || topics.some(topic => topic.length > 100)))
    return notify('请填写 1 至 10 个对比主题，每行一个且不超过 100 字')
  const sourceHeadings = sourceNames.slice(0, 7)
  outline.value = preparedQuestion.value && docMode.value !== '对比'
    ? [preparedQuestion.value]
    : docMode.value === '对比'
    ? topics
    : docMode.value === '流程'
      ? ['办理步骤', '责任与时限', ...sourceHeadings, '待核对事项']
      : docMode.value === '报告'
        ? ['背景与目标', ...sourceHeadings, '建议与后续行动']
        : [...sourceHeadings, '待核对事项']
  preparedQuestion.value = null
  draftReady.value = true
  if (sessionState.value === 'demo') { addTask(draftTitle.value, `${docMode.value}草稿`, sourceNames.length); return }
  draftArtifact.value = null
  workBusy.value = true
  try {
    draftArtifact.value = await createArtifact('docx', draftTitle.value, draftGoal.value, docSources.value.map(String), outline.value, 'brief', docMode.value === '对比' ? 'comparison' : 'standard')
    syncReviewEdits(draftArtifact.value)
    syncNoteEdits(draftArtifact.value)
    await refreshWorkspace()
    notify('提纲已保存，请核对后导出')
  } catch (error) { draftReady.value = false; notify(error instanceof Error ? error.message : '创建失败') }
  finally { workBusy.value = false }
}
async function generateSlides() {
  if (!slideTitle.value.trim() || !docs.value.some(doc => slideSources.value.includes(doc.id))) return notify('请填写主题并选择可见来源资料')
  if (sessionState.value === 'demo') { addTask(slideTitle.value, '演示文稿', slideSources.value.length); return }
  slideArtifact.value = null
  workBusy.value = true
  try {
    const suggested = await suggestSlideOutline(slideSources.value.map(String), slideCount.value)
    slideOutline.value = suggested.outline
    slideIndex.value = 0
    slideReady.value = true
    if (suggested.outline.length < slideCount.value)
      notify(`所选资料只有 ${suggested.outline.length} 个有依据的主题；已按实际主题数生成，可增加来源后再创建更多页面`)
    slideCount.value = suggested.outline.length
    slideArtifact.value = await createArtifact('pptx', slideTitle.value, slideAudience.value, slideSources.value.map(String), slideOutline.value, slideTheme.value)
    syncNoteEdits(slideArtifact.value)
    await refreshWorkspace()
    if (!collaborationAvailable.value) {
      try { slideArtifact.value = await generateSlideVisuals(slideArtifact.value.id, slideArtifact.value.draft_token) }
      catch { /* The evidence draft remains usable without matched photos. */ }
      notify('资料提纲已生成；AI 服务未配置，当前只能核对来源摘录')
      return
    }
    activeCollaborationId.value = slideArtifact.value.id
    try {
      const updated = await collaborateArtifact(slideArtifact.value.id, slideArtifact.value.draft_token, crypto.randomUUID())
      slideArtifact.value = updated
      syncNoteEdits(updated)
      await refreshCollaborationRun(updated)
      try { slideArtifact.value = await generateSlideVisuals(updated.id, updated.draft_token) }
      catch { notify('初稿已生成，配图暂不可用；可以稍后重新匹配') }
      await refreshWorkspace()
      notify(updated.section_drafts.some(section => section.ai_text)
        ? 'PPT 初稿已生成，请核对逐页内容与来源后导出'
        : '已生成资料提纲；部分页面证据不足，请补充内容后导出')
    } catch (error) {
      await refreshCollaborationRun(slideArtifact.value)
      notify(`提纲已保存，但 AI 初稿生成失败：${error instanceof Error ? error.message : '请稍后重试'}`)
    } finally {
      activeCollaborationId.value = null
    }
  } catch (error) { slideReady.value = false; notify(error instanceof Error ? error.message : '创建失败') }
  finally { workBusy.value = false }
}
async function refreshArtifactEvidence(item: Artifact | null, headings: string[]) {
  if (!item || item.status === 'completed') return
  workBusy.value = true
  try {
    const preview = await previewArtifact(item.id, headings)
    if (item.kind === 'docx') {
      draftArtifact.value = preview
      if (preview.mode === 'comparison') comparisonTopics.value = preview.outline.join('\n')
      syncReviewEdits(preview)
      syncNoteEdits(preview)
    }
    else { slideArtifact.value = preview; syncNoteEdits(preview) }
    notify('章节证据已更新，请核对摘录与缺口')
  } catch (error) { notify(error instanceof Error ? error.message : '证据更新失败') }
  finally { workBusy.value = false }
}
async function runArtifactCollaboration(item: Artifact | null, requestedId?: string) {
  if (!item || item.status !== 'pending' || item.mode !== 'standard') return
  if (collaborationRetryExhausted(item))
    return notify('本轮 AI 协作已达重试上限，请重新创建任务')
  if ((item.kind === 'docx' && (docNotesDirty.value || !draftEvidenceCurrent.value || draftInputsChanged.value)) ||
      (item.kind === 'pptx' && (slideNotesDirty.value || !slideEvidenceCurrent.value || slideInputsChanged.value)))
    return notify('请先保存当前改动并更新证据')
  workBusy.value = true
  activeCollaborationId.value = item.id
  try {
    const previous = collaborationRuns.value[item.id]
    const requestId = requestedId ?? (previous?.status === 'failed' && previous.can_retry && previous.draft_token === item.draft_token
      ? previous.request_id : crypto.randomUUID())
    const updated = await collaborateArtifact(item.id, item.draft_token, requestId)
    if (updated.kind === 'docx') draftArtifact.value = updated
    else {
      slideArtifact.value = updated
      try { slideArtifact.value = await generateSlideVisuals(updated.id, updated.draft_token) }
      catch { notify('AI 初稿已更新，配图暂不可用；可以稍后重新匹配') }
    }
    await refreshCollaborationRun(updated)
    notify(updated.kind === 'pptx'
      ? 'PPT 初稿已更新，请核对逐页内容与来源'
      : updated.reflection_trace.length ? 'AI 协作与反思已完成，请核对原文、引句和修订建议'
        : 'AI 协作草稿已生成，请核对原文、引句和建议文字')
  } catch (error) {
    await refreshCollaborationRun(item)
    notify(error instanceof Error ? error.message : 'AI 协作整理失败')
  }
  finally { activeCollaborationId.value = null; workBusy.value = false }
}
async function refreshSlideVisuals(item: Artifact | null) {
  if (!item || item.status !== 'pending' || !slideEvidenceCurrent.value || slideInputsChanged.value || slideNotesDirty.value) return
  workBusy.value = true
  try {
    const updated = await generateSlideVisuals(item.id, item.draft_token)
    slideArtifact.value = updated
    const onlineCount = updated.slide_visuals.filter(visual => visual.source === 'online').length
    notify(onlineCount ? `已为 ${onlineCount} 页匹配在线图片，请核对是否适合内容` :
      onlineSlideImagesAvailable.value ? '在线图库暂不可用，已按页主题选用本地配图' :
        '已按页主题选用本地配图；配置 Unsplash Access Key 后可检索在线图片')
  } catch (error) { notify(error instanceof Error ? error.message : '配图匹配失败') }
  finally { workBusy.value = false }
}
async function runArtifactReflection(item: Artifact | null) {
  if (!item || item.status !== 'pending' || item.mode !== 'standard' || !reflectionAvailable.value) return
  if (!item.section_drafts.some(section => section.ai_text)) return notify('请先运行 AI 协作整理')
  if ((item.kind === 'docx' && (docNotesDirty.value || !draftEvidenceCurrent.value || draftInputsChanged.value)) ||
      (item.kind === 'pptx' && (slideNotesDirty.value || !slideEvidenceCurrent.value || slideInputsChanged.value)))
    return notify('请先保存当前改动并更新证据')
  workBusy.value = true
  try {
    const updated = await reflectArtifact(item.id, item.draft_token, crypto.randomUUID())
    if (updated.kind === 'docx') { draftArtifact.value = updated; syncNoteEdits(updated) }
    else {
      slideArtifact.value = updated
      syncNoteEdits(updated)
      try { slideArtifact.value = await generateSlideVisuals(updated.id, updated.draft_token) }
      catch { /* Reflection text is saved even if photo matching fails. */ }
    }
    notify(updated.reflection_trace.at(-1)?.status === 'needs_review' ? '反思已完成，仍需人工核对' : 'AI 反思已完成，请核对修订内容')
  } catch (error) { notify(error instanceof Error ? error.message : 'AI 反思失败') }
  finally { workBusy.value = false }
}
async function saveComparisonReview() {
  const item = draftArtifact.value
  if (!item || item.mode !== 'comparison' || !draftEvidenceCurrent.value) return notify('请先更新并核对章节证据')
  if (reviewEdits.value.some((edit, index) => !edit.decision ||
      (item.section_drafts[index].evidence_gap && edit.decision !== 'unresolved') ||
      (edit.decision === 'different' && !edit.note.trim())))
    return notify('请为每个主题选择结论；发现差异时填写备注')
  workBusy.value = true
  try {
    draftArtifact.value = await reviewArtifact(item.id, reviewEdits.value as ComparisonReview[])
    syncReviewEdits(draftArtifact.value)
    notify('人工核对意见已保存')
  } catch (error) { notify(error instanceof Error ? error.message : '核对意见保存失败') }
  finally { workBusy.value = false }
}
async function exportArtifact(item: Artifact | null, headings: string[]) {
  if (sessionState.value === 'demo') return notify('界面演示不生成文件，请登录测试账号')
  if (!item) return notify('请先创建草稿')
  if (item.status === 'pending' && !item.section_drafts.every((section, index) => section.heading === headings[index]) )
    return notify('章节已修改，请先更新证据并核对')
  if (item.mode === 'comparison' && item.status === 'pending' && !comparisonReviewSaved.value)
    return notify('请先保存每个主题的人工核对结论')
  if (item.status === 'pending' && (item.kind === 'docx' ? docNotesDirty.value : slideNotesDirty.value))
    return notify('请先保存人工补充内容')
  workBusy.value = true
  try {
    if (item.kind === 'pptx' && item.status === 'pending' && !item.slide_visuals.length) {
      item = await generateSlideVisuals(item.id, item.draft_token)
      slideArtifact.value = item
    }
    const completed = await confirmArtifact(item.id, headings, item.draft_token)
    if (item.kind === 'docx') draftArtifact.value = completed
    else slideArtifact.value = completed
    await downloadArtifact(item.id, item.kind)
    await refreshWorkspace()
    notify('文件已生成并下载')
  } catch (error) { notify(error instanceof Error ? error.message : '导出失败') }
  finally { workBusy.value = false }
}
function toggleSource(list: (string | number)[], id: string | number) {
  const index = list.indexOf(id)
  if (index < 0) list.push(id)
  else list.splice(index, 1)
}
function handleFile(event: Event) {
  const input = event.target as HTMLInputElement
  selectedFile.value = input.files?.[0] ?? null
  if (selectedFile.value) uploadName.value = selectedFile.value.name.replace(/\.[^.]+$/, '')
}
async function submitUpload() {
  if (!selectedFile.value || !uploadName.value.trim()) return notify('请选择文件并填写资料名称')
  const ext = selectedFile.value.name.split('.').pop()?.toUpperCase()
  if (sessionState.value === 'ready') {
    workBusy.value = true
    try {
      await uploadDocument(selectedFile.value, uploadName.value.trim(), uploadArea.value, uploadVisibility.value)
      uploadOpen.value = false
      selectedFile.value = null
      await refreshWorkspace()
      notify('资料已上传，等待审核发布')
    } catch (error) { notify(error instanceof Error ? error.message : '上传失败') }
    finally { workBusy.value = false }
    return
  }
  if (ext !== 'PDF' && ext !== 'DOCX' && ext !== 'PPTX') return notify('演示列表仅支持 PDF、DOCX、PPTX')
  docs.value.unshift({ id: Date.now(), title: uploadName.value.trim(), type: ext, area: uploadArea.value, visibility: uploadVisibility.value, version: 'v1.0', date: '刚刚', owner: '林晓宇', excerpt: '演示资料尚未经过服务端解析与审核。', page: '待解析' })
  uploadOpen.value = false
  selectedFile.value = null
  uploadName.value = ''
  notify('已加入本地演示列表；正式上传需接入后端')
}
const taskFilter = ref<'全部' | WorkTask['status']>('全部')
const visibleTasks = computed(() => taskFilter.value === '全部' ? tasks.value : tasks.value.filter(task => task.status === taskFilter.value))
const taskPage = ref(1)
const taskPageSize = 10
const pagedTasks = computed(() => visibleTasks.value.slice(
  (taskPage.value - 1) * taskPageSize, taskPage.value * taskPageSize))
watch(taskFilter, () => { taskPage.value = 1 })
watch(visibleTasks, items => {
  taskPage.value = Math.min(taskPage.value, Math.max(1, Math.ceil(items.length / taskPageSize)))
})
</script>

<template>
  <div v-if="sessionState === 'login' || sessionState === 'loading'" class="auth-screen">
    <div class="auth-brand"><div class="brand-icon">知</div><div><strong>知序</strong><span>企业知识工作台</span></div></div>
    <form v-if="sessionState === 'login'" class="auth-form" @submit.prevent="submitLogin">
      <h1>登录工作台</h1>
      <p>使用本地开发账号</p>
      <label class="field-label">账号<input v-model="username" autocomplete="username" required /></label>
      <label class="field-label">密码<span class="password-field"><input v-model="password" :type="showPassword ? 'text' : 'password'" autocomplete="current-password" required /><button type="button" :aria-label="showPassword ? '隐藏密码' : '显示密码'" :aria-pressed="showPassword" :title="showPassword ? '隐藏密码' : '显示密码'" @click="showPassword = !showPassword"><EyeOff v-if="showPassword" :size="17" /><Eye v-else :size="17" /></button></span></label>
      <div v-if="loginError" class="auth-error" role="alert">{{ loginError }}</div>
      <button class="button button-primary auth-submit" type="submit" :disabled="loginBusy">{{ loginBusy ? '正在登录' : '登录' }}<ArrowRight :size="16" /></button>
      <button class="button button-quiet auth-demo" type="button" @click="enterDemo">查看界面演示</button>
    </form>
    <div v-else class="auth-loading" role="status">正在检查会话...</div>
  </div>
  <div v-else class="app-frame" :class="{ 'assistant-view': currentView === 'assistant', 'resizing-sidebar': sidebarResizing }" :style="{ '--sidebar-width': `${sidebarWidth}px` }">
    <div v-if="mobileNavOpen" class="mobile-scrim" @click="mobileNavOpen = false"></div>
    <aside class="sidebar" :class="{ open: mobileNavOpen }">
      <div class="brand"><div class="brand-icon">知</div><div><strong>知序</strong><span>企业知识工作台</span></div><button class="icon-btn close-nav" aria-label="关闭导航" title="关闭导航" @click="mobileNavOpen = false"><X :size="18" /></button></div>
      <nav aria-label="主导航">
        <div class="nav-label">工作空间</div>
        <template v-for="item in navMain" :key="item.id">
          <button class="nav-link" :class="{ active: currentView === item.id }" :aria-expanded="item.id === 'assistant' ? currentView === 'assistant' && chatHistoryOpen : undefined" @click="selectMainNav(item.id)"><component :is="item.icon" :size="18" /><span>{{ item.label }}</span><ChevronDown v-if="item.id === 'assistant'" :size="15" class="nav-chevron" :class="{ open: currentView === 'assistant' && chatHistoryOpen }" /></button>
          <div v-if="item.id === 'assistant' && currentView === 'assistant' && chatHistoryOpen" class="nav-chat-history" role="group" aria-label="最近会话">
            <span class="nav-subheading">最近会话</span>
            <div v-if="sessionState === 'ready'" class="conversation-history">
              <div v-for="conversation in conversations" :key="conversation.id" class="history-row" :class="{ active: activeConversationId === conversation.id }">
                <button class="history-item" :disabled="questionBusy || conversationDeleteBusy" :title="conversation.title" @click="openConversation(conversation.id)"><MessageSquareText :size="14" /><span>{{ conversation.title }}</span></button>
                <button class="history-delete" type="button" :disabled="questionBusy || conversationDeleteBusy || conversationLoading" :aria-label="`删除会话：${conversation.title}`" :title="`删除会话：${conversation.title}`" @click="conversationToDelete = conversation"><Trash2 :size="14" /></button>
              </div>
              <span v-if="conversations.length === 0" class="history-empty">暂无会话</span>
            </div>
            <span v-else class="history-empty">演示会话</span>
          </div>
        </template>
        <div class="nav-label nav-label-spaced">管理</div>
        <button v-for="item in navManage" :key="item.id" class="nav-link" :class="{ active: currentView === item.id }" @click="navigate(item.id)"><component :is="item.icon" :size="18" /><span>{{ item.label }}</span><span v-if="item.id === 'tasks'" class="nav-counter">{{ tasks.length }}</span></button>
      </nav>
      <div class="sidebar-foot"><div class="demo-note"><LockKeyhole :size="14" />{{ sessionState === 'demo' ? '界面演示数据' : '本地开发环境' }}</div><div class="profile"><div class="profile-avatar">{{ currentUser?.user_id.slice(0, 1).toUpperCase() ?? '演' }}</div><div><strong>{{ currentUser?.user_id ?? '界面演示' }}</strong><span>{{ currentUser ? `${roleNames[currentUser.role]} · ${currentUser.org_id}` : '未连接企业资料' }}</span></div><button class="icon-btn" :aria-label="currentUser ? '退出登录' : '返回登录'" :title="currentUser ? '退出登录' : '返回登录'" @click="signOut"><LogOut :size="17" /></button></div></div>
      <div class="sidebar-resize-handle" role="separator" aria-label="调整导航宽度" aria-orientation="vertical" :aria-valuenow="sidebarWidth" aria-valuemin="190" aria-valuemax="420" tabindex="0" title="拖动调整导航宽度" @pointerdown="startSidebarResize" @keydown="resizeSidebarByKey"></div>
    </aside>

    <div class="main-frame">
      <header v-if="currentView !== 'assistant'" class="topbar"><div class="crumb"><button class="icon-btn mobile-menu" aria-label="打开导航" title="打开导航" @click="mobileNavOpen = true"><Menu :size="20" /></button><span>企业空间</span><ChevronRight :size="14" /><strong>{{ labels[currentView] }}</strong></div><div class="top-actions"><span v-if="currentView !== 'documents'" class="environment-pill">资料演示</span><button class="icon-btn" aria-label="通知" title="通知" @click="notify('暂无新通知')"><Bell :size="18" /></button><div class="header-avatar">{{ currentUser?.user_id.slice(0, 1).toUpperCase() ?? '演' }}</div></div></header>

      <main>
        <div v-if="currentView === 'library' && sessionState === 'ready'" class="catalog-status" role="status">
          <span v-if="documentsBusy">正在加载授权资料...</span>
          <span v-else-if="documentsError">{{ documentsError }}</span>
          <span v-else>当前账号可见 {{ docs.length }} 份本地合成资料</span>
          <button type="button" :disabled="documentsBusy" @click="refreshDocuments">刷新</button>
        </div>
        <div v-if="currentView === 'library' && sessionState === 'ready' && (pendingDocuments.length || pendingVersions.length)" class="pending-strip">
          <strong>待审核资料</strong>
          <div v-for="item in pendingDocuments" :key="item.id"><span>{{ item.title }} · {{ item.visibility }}</span><button type="button" @click="approveDocument(item.id)">发布</button></div>
          <div v-for="item in pendingVersions" :key="`${item.document_id}:${item.version}`"><span>{{ item.title }} · {{ item.version }} 新版本</span><button type="button" @click="approveVersion(item.document_id)">发布新版本</button></div>
        </div>
        <div v-if="currentView === 'tasks' && sessionState === 'ready' && artifacts.length" class="artifact-strip">
          <strong>已保存的生成任务 <small>{{ artifacts.length }} 项</small></strong>
          <div class="artifact-tools"><label class="artifact-search"><Search :size="15" /><input v-model="artifactSearch" type="search" placeholder="搜索任务名称或目标" aria-label="搜索已保存的生成任务" /></label><div class="artifact-status-filters" role="group" aria-label="筛选生成任务状态"><button v-for="status in artifactStatusOptions" :key="status" type="button" :class="{ active: artifactStatusFilter === status }" :aria-pressed="artifactStatusFilter === status" @click="artifactStatusFilter = status">{{ status }}</button></div></div>
          <div v-for="item in pagedArtifacts" :key="item.id" class="artifact-row"><span :title="item.title">{{ item.title }} · {{ item.kind.toUpperCase() }} · {{ item.source_status === 'stale' ? '来源已更新' : item.source_status === 'unverified' ? '来源版本未记录' : item.status === 'completed' ? '已完成' : '待确认' }}</span><button type="button" :disabled="item.source_status === 'stale'" @click="resumeArtifact(item)">{{ item.status === 'completed' ? '查看' : '继续编辑' }}</button></div>
          <p v-if="!filteredArtifacts.length" class="artifact-empty">没有匹配的生成任务</p>
          <ListPager :page="artifactPage" :total="filteredArtifacts.length" :page-size="artifactPageSize" @change="artifactPage = $event" />
        </div>
        <section v-if="currentView === 'assistant'" class="page chat-page">
          <div class="chat-shell"><div class="conversation">
              <div class="conversation-top"><div class="conversation-heading"><button class="icon-btn mobile-menu" aria-label="打开导航" title="打开导航" @click="mobileNavOpen = true"><Menu :size="20" /></button><div class="conversation-workspace" aria-label="当前企业空间：星图科技"><strong>星图科技</strong><span>企业空间</span></div></div><div class="conversation-controls"><button class="button button-quiet new-chat-button" :disabled="questionBusy" @click="newChat"><Plus :size="16" />新建会话</button></div></div>
              <div ref="messageList" class="message-list">
                <div v-if="messages.length === 0" class="empty-conversation"><div class="assistant-glyph"><Sparkles :size="23" /></div><h2>从一个问题开始</h2><p>可以直接提问，也可以选择文件后问答或修改。</p><div class="quick-questions"><button @click="ask('出差报销需要哪些审批？')">出差报销流程 <ArrowRight :size="14" /></button><button @click="ask('工作满3年有多少年假？')">年假有多少天 <ArrowRight :size="14" /></button></div></div>
                <template v-for="(message, index) in messages" :key="index">
                  <div v-if="message.role === 'user'" class="user-bubble">{{ message.text }}</div>
                  <div v-else class="assistant-answer"><span class="answer-avatar">知</span><div>
                    <strong>{{ message.mode === 'artifact_edit' ? '文件修改' : message.mode === 'file_access' ? '文件权限' : message.mode === 'file_answer' ? '所选文件问答' : message.mode === 'general' ? '普通对话' : message.answerStatus === 'needs_clarification' ? '补充条件' : message.answerStatus === 'answered' ? '制度条件问答' : message.mode === 'gpt' ? 'GPT 引句' : message.mode === 'demo' ? '演示资料回答' : '授权资料原文摘录' }}</strong>
                    <div v-if="(message.mode === 'general' || message.mode === 'file_access') && !message.error" class="answer-markdown" v-html="renderGeneralAnswer(message.paragraphs)"></div>
                    <template v-else><p v-for="(paragraph, p) in message.paragraphs" :key="p">{{ paragraph }}</p></template>
                    <div v-if="message.edit" class="edit-proposal"><div class="edit-proposal-title"><FileText :size="16" /><strong>{{ message.edit.artifact_title }}</strong><span>{{ message.edit.kind.toUpperCase() }} · {{ message.edit.heading }}</span></div><div class="edit-diff"><div><small>修改前</small><p>{{ message.edit.before_text }}</p></div><div><small>修改后</small><p>{{ message.edit.revised_text }}</p></div></div><p class="edit-evidence">依据原文：{{ message.edit.source_quote }}</p><div class="edit-proposal-actions"><span v-if="message.edit.status === 'applied'">已保存到草稿</span><span v-else-if="message.edit.status === 'stale'">来源已变化，需重新创建草稿</span><span v-else-if="message.edit.status === 'outdated'">草稿已变化，请重新提出修改</span><button v-else class="button button-primary" type="button" :disabled="questionBusy" @click="applyChatEdit(index, message.edit)"><Check :size="14" />确认修改</button><button type="button" class="button button-quiet" @click="artifacts.find(item => item.id === message.edit?.artifact_id) && resumeArtifact(artifacts.find(item => item.id === message.edit?.artifact_id)!)">查看草稿</button></div></div>
                    <button v-for="(citation, c) in message.citations" :key="c" class="inline-citation" @click="selectedDoc = citation">[{{ c + 1 }}] {{ citation.title }}</button>
                    <button v-if="sessionState === 'ready' && message.clarification && !message.streaming && !message.error" class="answer-retry clarification-trigger" type="button" :disabled="questionBusy" @click="openClarification(index)">补充{{ message.clarification.field }} <ArrowRight :size="14" /></button>
                    <button v-if="sessionState === 'ready' && !message.streaming && !message.error && availableDocumentActions(index).manual" class="answer-retry" type="button" @click="prepareDocumentFromAnswer(index)"><FilePlus2 :size="14" />手动整理文档</button><button v-if="sessionState === 'ready' && collaborationAvailable && activeConversationId && !message.streaming && !message.error && availableDocumentActions(index).generate" class="answer-retry" type="button" :disabled="workBusy" :title="composingAnswerKey === `answer-doc-${activeConversationId}-${Math.floor(index / 2)}` ? '正在依据当前授权引用生成待核对 DOCX 草稿' : '依据当前授权引用生成待核对 DOCX 草稿'" @click="composeDocumentFromAnswer(index)"><LoaderCircle v-if="composingAnswerKey === `answer-doc-${activeConversationId}-${Math.floor(index / 2)}`" class="answer-wait-icon" :size="14" aria-hidden="true" /><Sparkles v-else :size="14" />{{ composingAnswerKey === `answer-doc-${activeConversationId}-${Math.floor(index / 2)}` ? '正在生成草稿' : '生成文档草稿' }}</button>
                    <p v-if="message.draftError" class="answer-action-error" role="alert">草稿未生成：{{ message.draftError }}</p>
                    <div class="answer-disclaimer" :class="{ processing: message.streaming }" role="status"><LoaderCircle v-if="message.streaming" class="answer-wait-icon" :size="14" aria-hidden="true" /><ShieldCheck v-else :size="14" aria-hidden="true" />{{ message.streaming ? '正在思考' : message.error ? '本次请求未完成' : message.mode === 'artifact_edit' ? 'AI 修改需人工确认，原资料不会被覆盖' : message.mode === 'file_access' ? '依据当前账号的文件权限，操作时会重新核验' : message.mode === 'general' ? '普通对话未使用企业资料' : message.answerStatus === 'needs_clarification' ? '补充条件后继续依据授权资料回答' : message.mode === 'file_answer' ? '仅依据所选文件，请核对内容' : message.citations?.length ? '请核对引用原文' : '未找到足够依据' }}</div>
                    <button v-if="message.error && message.retry" class="answer-retry" type="button" :disabled="questionBusy" @click="retryAnswer(index)">重试回答 <ArrowRight :size="14" /></button>
                  </div></div>
                </template>
              </div>
              <form class="composer" @submit.prevent="ask()">
                <textarea v-model="question" rows="2" :disabled="conversationLoading || questionBusy" :placeholder="editingArtifact || editingSource ? `询问《${(editingArtifact || editingSource)?.title}》，或描述要做的修改` : '输入问题，或选择文件后提问或修改'" aria-label="输入问题或文件修改要求" @keydown="onQuestionKeydown"></textarea>
                <div class="composer-actions">
                  <details v-if="sessionState === 'ready'" ref="editMenu" class="scope-menu edit-menu" @toggle="onEditMenuToggle" @keydown.esc.stop.prevent="editMenu && (editMenu.open = false)">
                    <summary aria-label="选择文件" title="选择文件"><FileText :size="16" /><span>{{ editingArtifact?.title || editingSource?.title || '选择文件' }}</span><ChevronDown :size="14" /></summary>
                    <div class="scope-menu-panel edit-menu-panel">
                      <div class="edit-menu-search"><Search :size="16" /><input ref="fileSearchInput" v-model="fileSearch" type="search" placeholder="搜索文件名、类型或部门" aria-label="搜索文件" @keydown.enter.prevent /></div>
                      <div class="edit-menu-results">
                        <button v-if="editingArtifactId || editingSourceId" class="edit-menu-clear" type="button" @click="clearEditTarget">取消选择</button>
                        <div v-if="filteredEditableArtifacts.length" class="scope-menu-heading">我的草稿 <small>{{ filteredEditableArtifacts.length }} 份</small></div>
                        <div v-for="item in filteredEditableArtifacts" :key="item.id" class="edit-menu-row" :class="{ selected: editingArtifactId === item.id }">
                          <button class="edit-menu-select" type="button" :aria-pressed="editingArtifactId === item.id" @click="selectEditArtifact(item.id)"><span>{{ item.title }}</span><small>{{ item.kind.toUpperCase() }} · {{ item.source_ids.length }} 份来源</small></button>
                          <button class="edit-menu-preview" type="button" :aria-label="`预览 ${item.title}`" :title="`预览 ${item.title}`" @click="openEditPreview('artifact', item.id, item.title)"><Eye :size="16" /></button>
                        </div>
                        <div v-if="filteredSourceDocs.length" class="scope-menu-heading">可见资料 <small>{{ filteredSourceDocs.length }} 份</small></div>
                        <div v-for="item in filteredSourceDocs" :key="item.id" class="edit-menu-row" :class="{ selected: editingSourceId === String(item.id) }">
                          <button class="edit-menu-select" type="button" :aria-pressed="editingSourceId === String(item.id)" @click="selectEditSource(String(item.id))"><span>{{ item.title }}</span><small>{{ item.type }} · {{ item.area }}</small></button>
                          <button class="edit-menu-preview" type="button" :aria-label="`预览 ${item.title}`" :title="`预览 ${item.title}`" @click="openEditPreview('source', String(item.id), item.title)"><Eye :size="16" /></button>
                        </div>
                        <p v-if="!filteredEditableArtifacts.length && !filteredSourceDocs.length" class="edit-menu-empty">没有匹配的文件</p>
                      </div>
                    </div>
                  </details>
                  <button v-if="editingArtifactId || editingSourceId" class="clear-edit-target" type="button" aria-label="取消选择文件" title="取消选择文件" @click="clearEditTarget"><X :size="15" /></button>
                  <select v-if="sessionState === 'ready' && gptAvailable && !editingArtifact && !editingSource" v-model="answerMode" class="composer-mode" :disabled="questionBusy" aria-label="回答方式" title="回答方式"><option value="extract">原文摘录</option><option value="gpt">GPT 引句</option></select>
                  <button class="send-button" type="submit" :disabled="questionBusy || conversationLoading" aria-label="发送问题" title="发送问题"><ArrowUp :size="18" /></button>
                </div>
              </form>
            </div>
            </div>
        </section>

        <section v-else-if="currentView === 'library'" class="page standard-page library-page">
          <div class="stats-row"><div><span>可见资料</span><strong>{{ docs.length }}</strong></div><div><span>制度规范</span><strong>{{ docs.filter(d => d.area === '制度规范').length }}</strong></div><div><span>产品资料</span><strong>{{ docs.filter(d => d.area === '产品资料').length }}</strong></div><div><span>项目空间</span><strong>{{ docs.filter(d => d.area === '项目空间').length }}</strong></div></div>
          <div class="toolbar"><div class="search-field"><Search :size="17" /><input v-model="search" type="search" placeholder="搜索文件名或负责人" aria-label="搜索资料" /></div><details ref="categoryMenu" class="filter-menu" @keydown.esc.stop.prevent="categoryMenu && (categoryMenu.open = false)"><summary aria-label="资料分类" @click="visibilityMenu && (visibilityMenu.open = false)"><span>{{ category }}</span><ChevronDown :size="15" /></summary><div class="filter-menu-panel" role="group" aria-label="资料分类选项"><div class="filter-menu-title">资料分类</div><button v-for="item in categoryOptions" :key="item" type="button" :class="{ selected: category === item }" :aria-pressed="category === item" @click="selectCategory(item)"><span>{{ item }}</span><small>{{ item === '全部分类' ? docs.length : docs.filter(doc => doc.area === item).length }}</small><Check v-if="category === item" :size="15" /></button></div></details><details ref="visibilityMenu" class="filter-menu" @keydown.esc.stop.prevent="visibilityMenu && (visibilityMenu.open = false)"><summary aria-label="可见范围" @click="categoryMenu && (categoryMenu.open = false)"><span>{{ visibility }}</span><ChevronDown :size="15" /></summary><div class="filter-menu-panel" role="group" aria-label="可见范围选项"><div class="filter-menu-title">可见范围</div><button v-for="item in visibilityOptions" :key="item" type="button" :class="{ selected: visibility === item }" :aria-pressed="visibility === item" @click="selectVisibility(item)"><span>{{ item }}</span><small>{{ item === '全部权限' ? docs.length : docs.filter(doc => doc.visibility === item).length }}</small><Check v-if="visibility === item" :size="15" /></button></div></details><button v-if="sessionState === 'demo' || currentUser?.role === 'department_manager' || currentUser?.role === 'knowledge_admin'" class="button button-primary upload-button" @click="uploadOpen = true"><Upload :size="16" />上传资料</button></div>
          <div class="data-surface">
            <table>
              <thead><tr><th>资料名称</th><th>知识空间</th><th>可见范围</th><th>版本</th><th>原件</th><th>更新</th><th></th></tr></thead>
              <tbody>
                <tr v-for="doc in pagedDocs" :key="doc.id">
                  <td><div class="file-cell"><span class="file-tile" :class="doc.type.toLowerCase()"><Presentation v-if="doc.type === 'PPTX'" :size="18" /><FileText v-else :size="18" /></span><span><strong>{{ doc.title }}</strong><small>{{ doc.type }} · {{ doc.owner }}</small></span></div></td>
                  <td>{{ doc.area }}</td>
                  <td><span class="visibility-tag" :class="doc.visibility === '机密' ? 'secret' : doc.visibility === '部门受限' ? 'department' : ''">{{ doc.visibility }}</span></td>
                  <td>{{ doc.version }}</td>
                  <td><span class="original-status" :class="{ available: doc.has_original }">{{ doc.has_original ? '可下载' : '无原件' }}</span></td>
                  <td>{{ doc.date }}</td>
                  <td><button class="link-button" @click="selectedDoc = doc">查看 <ArrowRight :size="14" /></button></td>
                </tr>
              </tbody>
            </table>
            <div v-if="filteredDocs.length === 0" class="table-empty"><Search :size="26" /><strong>没有找到匹配资料</strong><span>试试调整搜索词或筛选条件</span></div>
            <ListPager :page="libraryPage" :total="filteredDocs.length" :page-size="libraryPageSize" @change="libraryPage = $event" />
          </div>
        </section>

        <DepartmentMeeting v-else-if="currentView === 'documents'" :user="sessionState === 'ready' ? currentUser : null" />
        <section v-else-if="currentView === 'doc_editor'" class="page standard-page">
          <div class="creation-grid"><div class="creation-form">
            <div class="step"><span class="step-num">02</span><div><h2>来源资料 <small>已选 {{ docSources.length }} 份</small></h2><label class="source-search"><Search :size="14" /><input v-model="docSourceSearch" type="search" placeholder="查找来源资料" aria-label="查找文档来源" /></label><div class="source-list"><label v-for="doc in pagedDocSourceChoices" :key="doc.id" class="source-row"><input type="checkbox" :checked="docSources.includes(doc.id)" @change="toggleSource(docSources, doc.id)" /><span><strong>{{ doc.title }}</strong><small>{{ doc.area }} · {{ doc.version }}</small></span><span class="visibility-tag compact" :class="doc.visibility === '机密' ? 'secret' : doc.visibility === '部门受限' ? 'department' : ''">{{ doc.visibility }}</span></label><p v-if="!docSourceChoices.length" class="source-empty">没有匹配的资料</p></div><ListPager :page="docSourcePage" :total="docSourceChoices.length" :page-size="sourcePageSize" @change="docSourcePage = $event" /></div></div>
            <div class="step"><span class="step-num">03</span><div><h2>任务要求</h2><label class="field-label">文档标题<input v-model="draftTitle" /></label><label class="field-label">整理重点<textarea v-model="draftGoal" rows="3"></textarea></label><label v-if="docMode === '对比'" class="field-label">对比主题（每行一个）<textarea v-model="comparisonTopics" rows="4" /></label><div class="form-end"><button class="button button-primary" :disabled="workBusy" @click="generateDraft"><Sparkles :size="16" />生成章节草稿</button></div></div></div>
            </div><div class="preview-panel"><div class="preview-header"><div><span>草稿预览</span><strong>{{ draftArtifact?.title || draftTitle || '未命名文档' }}</strong></div><button v-if="draftArtifact?.status === 'pending' && draftArtifact.mode === 'standard'" class="button button-quiet" :disabled="!collaborationAvailable || workBusy || !draftEvidenceCurrent || draftInputsChanged || docNotesDirty || collaborationRetryExhausted(draftArtifact)" title="将所选资料的授权摘录发送到已配置的模型服务" @click="runArtifactCollaboration(draftArtifact)"><Sparkles :size="15" />{{ collaborationRuns[draftArtifact.id]?.status === 'failed' && collaborationRuns[draftArtifact.id]?.draft_token === draftArtifact.draft_token ? collaborationRuns[draftArtifact.id]?.can_retry ? '继续 AI 协作' : '已达重试上限' : 'AI 协作整理' }}</button><button v-if="draftArtifact?.status === 'pending' && draftArtifact.mode === 'standard' && draftArtifact.section_drafts.some(section => section.ai_text) && !draftArtifact.reflection_trace.length" class="button button-quiet" :disabled="!reflectionAvailable || workBusy || !draftEvidenceCurrent || draftInputsChanged || docNotesDirty" title="让 Critic 检查 AI 建议并限次修订" @click="runArtifactReflection(draftArtifact)"><Brain :size="15" />AI 反思</button><span class="status-badge">{{ draftArtifact?.status === 'completed' ? '已完成' : draftReady ? '待人工核对' : '待生成' }}</span></div>
            <p v-if="draftArtifact?.status === 'pending' && draftArtifact.mode === 'standard' && !draftArtifact.collaboration_trace.length" class="ai-consent">运行后，所选资料的授权摘录会发送到已配置的模型服务。</p>
            <AgentCollaborationTimeline v-if="sessionState === 'ready' && docMode !== '对比'" :artifact="draftArtifact" :run="draftArtifact ? collaborationRuns[draftArtifact.id] : undefined" :running="activeCollaborationId === draftArtifact?.id" :available="collaborationAvailable" />
            <div v-if="draftEvidenceCurrent && draftArtifact?.section_drafts.some(section => section.ai_text)" class="ai-suggestions"><div v-for="section in draftArtifact.section_drafts.filter(section => section.ai_text)" :key="section.heading"><strong>{{ section.heading }} · AI 建议</strong><p>{{ section.ai_text }}</p><template v-if="section.ai_evidence?.length"><small v-for="entry in section.ai_evidence" :key="entry.citation.document_id">{{ entry.citation.title }} {{ entry.citation.version }} · {{ entry.citation.location }}：{{ entry.quote }}</small></template><small v-else>依据原文：{{ section.ai_quote }}</small></div></div>
            <div class="paper"><span class="paper-rule"></span><template v-if="draftReady"><h2>{{ draftArtifact?.title || draftTitle }}</h2><p class="paper-meta">{{ draftArtifact?.source_ids.length ?? docSources.length }} 份来源资料</p><p class="paper-note">{{ draftArtifact?.goal ?? draftGoal }}</p><p v-if="draftInputsChanged" class="evidence-gap">表单内容已改变；此预览和导出仍属于已保存任务。请重新生成章节草稿。</p>
              <div v-for="(heading, index) in outline" :key="index" class="draft-section"><label class="outline-edit"><span>{{ String(index + 1).padStart(2, '0') }}</span><input v-model="outline[index]" :disabled="draftArtifact?.status === 'completed'" :aria-label="`章节 ${index + 1}`" /></label>
                <template v-if="draftEvidenceCurrent && draftArtifact?.section_drafts[index]"><template v-if="draftArtifact.section_drafts[index].sources"><div v-for="source in draftArtifact.section_drafts[index].sources" :key="source.document_id" class="comparison-source"><strong>{{ source.title }} · {{ source.version }}</strong><p v-if="source.evidence_gap" class="evidence-gap">此来源未找到与本主题直接相关的片段。</p><template v-else><p class="draft-excerpt">{{ source.text }}</p><p class="draft-citation">{{ source.citation?.location }}</p></template></div><p class="comparison-review">{{ draftArtifact.section_drafts[index].comparison_status === 'missing_evidence' ? '部分来源缺少匹配证据，需补充并人工核对。' : '各来源摘录待人工核对差异与冲突。' }}</p><div v-if="draftArtifact.status === 'pending' && reviewEdits[index]" class="comparison-review-form"><label class="field-label">人工核对结论<select v-model="reviewEdits[index].decision" :aria-label="`主题 ${index + 1} 核对结论`"><option value="">请选择</option><option value="aligned" :disabled="draftArtifact.section_drafts[index].evidence_gap">无显著差异</option><option value="different" :disabled="draftArtifact.section_drafts[index].evidence_gap">发现差异</option><option value="unresolved">证据不足，待核对</option></select></label><label class="field-label">核对备注<textarea v-model="reviewEdits[index].note" :aria-label="`主题 ${index + 1} 核对备注`" rows="2" maxlength="500"></textarea></label></div><p v-else-if="draftArtifact.section_drafts[index].review" class="comparison-human-note">人工核对：{{ draftArtifact.section_drafts[index].review?.decision === 'aligned' ? '无显著差异' : draftArtifact.section_drafts[index].review?.decision === 'different' ? '发现差异' : '证据不足，待核对' }}<span v-if="draftArtifact.section_drafts[index].review?.note"> · {{ draftArtifact.section_drafts[index].review?.note }}</span></p></template><p v-else-if="draftArtifact.section_drafts[index].evidence_gap" class="evidence-gap">待补充证据：所选资料未找到与本章节直接相关的片段。</p><template v-else><p class="draft-excerpt">{{ draftArtifact.section_drafts[index].text }}</p><p class="draft-citation">来源：{{ draftArtifact.section_drafts[index].citation?.title }} {{ draftArtifact.section_drafts[index].citation?.version }} · {{ draftArtifact.section_drafts[index].citation?.location }}</p></template></template>
                <div v-if="draftEvidenceCurrent && draftArtifact?.section_drafts[index]?.supporting_sources?.length && draftArtifact.section_drafts[index].supporting_sources!.length > 1" class="comparison-source"><strong>补充来源</strong><div v-for="source in draftArtifact.section_drafts[index].supporting_sources!.slice(1)" :key="source.citation.document_id"><p class="draft-excerpt">{{ source.text }}</p><p class="draft-citation">{{ source.citation.title }} {{ source.citation.version }} · {{ source.citation.location }}</p></div></div>
                <label v-if="draftEvidenceCurrent && draftArtifact?.status === 'pending'" class="field-label manual-note-field">人工补充<textarea v-model="docNoteEdits[index]" :aria-label="`章节 ${index + 1} 人工补充`" maxlength="1200" rows="3" placeholder="补充自己的核对意见或说明"></textarea></label>
                <p v-else-if="draftEvidenceCurrent && draftArtifact?.section_drafts[index]?.manual_note" class="manual-note-readonly">人工补充（{{ draftArtifact.section_drafts[index].manual_note_by }}）：{{ draftArtifact.section_drafts[index].manual_note }}</p>
              </div><p v-if="draftArtifact?.status === 'completed' && !draftArtifact.section_drafts.length" class="evidence-gap">旧文件未保存章节证据，可下载原文件核对。</p><p v-else-if="!draftEvidenceCurrent && sessionState !== 'demo'" class="evidence-gap">章节已修改，需更新证据后核对。</p>
            </template><div v-else class="paper-placeholder"><FileText :size="30" /><strong>章节草稿将在这里显示</strong><span>选择资料并填写整理要求后开始</span></div></div>
            <div class="preview-footer"><span>{{ draftReady ? '请核对摘录和来源' : '来源资料由服务端检查' }}</span><button v-if="draftReady && !draftEvidenceCurrent && draftArtifact?.status === 'pending'" class="button button-quiet" :disabled="workBusy" @click="refreshArtifactEvidence(draftArtifact, outline)">更新证据</button><button v-if="draftArtifact?.mode === 'comparison' && draftArtifact.status === 'pending' && draftEvidenceCurrent" class="button button-quiet" :disabled="workBusy || comparisonReviewSaved" @click="saveComparisonReview"><FileCheck2 :size="16" />{{ comparisonReviewSaved ? '已保存核对' : '保存核对意见' }}</button><button v-if="draftArtifact?.status === 'pending' && draftEvidenceCurrent" class="button button-quiet" :disabled="workBusy || !docNotesDirty" @click="saveNotes(draftArtifact)">{{ docNotesDirty ? '保存人工补充' : '补充已保存' }}</button><button class="button button-quiet" :disabled="!draftReady || workBusy || (sessionState !== 'demo' && !draftEvidenceCurrent && draftArtifact?.status !== 'completed')" @click="exportArtifact(draftArtifact, outline)"><ArrowDownToLine :size="16" />导出 DOCX</button></div>
          </div></div>
        </section>

        <section v-else-if="currentView === 'slides'" class="page standard-page">
          <div class="creation-grid slides-grid"><div class="creation-form"><div class="step"><span class="step-num">01</span><div><h2>汇报设置</h2><label class="field-label">主题<input v-model="slideTitle" /></label><div class="two-fields"><label class="field-label">受众<select v-model="slideAudience"><option>部门例会</option><option>管理层汇报</option><option>项目复盘</option></select></label><label class="field-label">页数<select v-model.number="slideCount"><option v-if="![6, 8, 10].includes(slideCount)" :value="slideCount">{{ slideCount }} 页</option><option :value="6">6 页</option><option :value="8">8 页</option><option :value="10">10 页</option></select></label></div></div></div><div class="step"><span class="step-num">02</span><div><h2>企业模板</h2><div class="theme-row"><button class="theme-option" :class="{ selected: slideTheme === 'brief' }" @click="slideTheme = 'brief'"><span class="theme-mini clear"><i></i><b></b></span>清晰简报</button><button class="theme-option" :class="{ selected: slideTheme === 'strategy' }" @click="slideTheme = 'strategy'"><span class="theme-mini strategy"><i></i><b></b></span>战略汇报</button></div></div></div><div class="step"><span class="step-num">03</span><div><h2>来源资料 <small>已选 {{ slideSources.length }} 份</small></h2><label class="source-search"><Search :size="14" /><input v-model="slideSourceSearch" type="search" placeholder="查找来源资料" aria-label="查找 PPT 来源" /></label><div class="source-list"><label v-for="doc in pagedSlideSourceChoices" :key="doc.id" class="source-row"><input type="checkbox" :checked="slideSources.includes(doc.id)" @change="toggleSource(slideSources, doc.id)" /><span><strong>{{ doc.title }}</strong><small>{{ doc.area }} · {{ doc.version }}</small></span></label><p v-if="!slideSourceChoices.length" class="source-empty">没有匹配的资料</p></div><ListPager :page="slideSourcePage" :total="slideSourceChoices.length" :page-size="sourcePageSize" @change="slideSourcePage = $event" /><div class="form-end"><button class="button button-primary" :disabled="workBusy" @click="generateSlides"><Sparkles :size="16" />{{ workBusy ? '正在生成初稿' : collaborationAvailable ? '生成 PPT 初稿' : '生成资料草稿' }}</button></div><p v-if="!collaborationAvailable" class="slides-model-note">AI 模型未连接时，只能生成资料摘录提纲。</p></div></div></div>
          <div class="preview-panel slide-preview"><div class="preview-header"><div><span>演示预览</span><strong>{{ slideArtifact?.title || slideTitle || '未命名演示' }}</strong></div><button v-if="slideArtifact?.status === 'pending'" class="button button-quiet" :disabled="!collaborationAvailable || workBusy || !slideEvidenceCurrent || slideInputsChanged || slideNotesDirty || collaborationRetryExhausted(slideArtifact)" title="根据所选资料生成或重新生成逐页 AI 初稿" @click="runArtifactCollaboration(slideArtifact)"><Sparkles :size="15" />{{ activeCollaborationId === slideArtifact.id ? '正在生成初稿' : collaborationRuns[slideArtifact.id]?.status === 'failed' && collaborationRuns[slideArtifact.id]?.draft_token === slideArtifact.draft_token ? collaborationRuns[slideArtifact.id]?.can_retry ? '继续生成初稿' : '已达重试上限' : slideArtifact.section_drafts.some(section => section.ai_text) ? '重新生成初稿' : '生成 AI 初稿' }}</button><button v-if="slideArtifact?.status === 'pending'" class="button button-quiet" :disabled="workBusy || !slideEvidenceCurrent || slideInputsChanged || slideNotesDirty" title="按逐页内容匹配图片；在线图库未配置时使用本地素材" @click="refreshSlideVisuals(slideArtifact)"><ImagePlus :size="15" />匹配配图</button><button v-if="slideArtifact?.status === 'pending' && slideArtifact.mode === 'standard' && slideArtifact.section_drafts.some(section => section.ai_text) && !slideArtifact.reflection_trace.length" class="button button-quiet" :disabled="!reflectionAvailable || workBusy || !slideEvidenceCurrent || slideInputsChanged || slideNotesDirty" title="让 Critic 检查 AI 初稿并限次修订" @click="runArtifactReflection(slideArtifact)"><Brain :size="15" />AI 反思</button><span class="status-badge">{{ slideArtifact?.status === 'completed' ? '已完成' : slideArtifact?.section_drafts.some(section => section.ai_text) ? 'AI 初稿待核对' : slideReady ? '待人工核对' : '待生成' }}</span></div>
            <p v-if="slideArtifact?.status === 'pending' && !slideArtifact.collaboration_trace.length" class="ai-consent">运行后，所选资料的授权摘录会发送到已配置的模型服务。</p>
            <AgentCollaborationTimeline v-if="sessionState === 'ready'" :artifact="slideArtifact" :run="slideArtifact ? collaborationRuns[slideArtifact.id] : undefined" :running="activeCollaborationId === slideArtifact?.id" :available="collaborationAvailable" />
            <div v-if="slideEvidenceCurrent && activeSlideDraft?.ai_text" class="ai-suggestions"><strong>本页初稿依据 · 请核对</strong><template v-if="activeSlideDraft.ai_evidence?.length"><small v-for="(entry, index) in activeSlideDraft.ai_evidence" :key="`${entry.citation.document_id}-${index}`">{{ entry.citation.title }} {{ entry.citation.version }} · {{ entry.citation.location }}：{{ entry.quote }}</small></template><small v-else>依据原文：{{ activeSlideDraft.ai_quote }}</small></div>
            <p v-if="slideInputsChanged" class="evidence-gap slide-warning">表单内容已改变；此预览和导出仍属于已保存任务。请重新生成逐页草稿。</p>
            <div class="slide-canvas" :class="[slidePreviewLayout, { dark: slidePreviewDark, reverse: slidePreviewReverse, strategy: slidePreviewTheme === 'strategy' }]">
              <img v-if="slidePreviewLayout !== 'focus'" class="slide-photo" :class="{ 'source-visual': activeSlideVisual?.source === 'document' }" :src="slidePreviewImage" :alt="activeSlideVisual?.source === 'document' ? '所选资料中的配图' : '通用商务场景配图'" />
              <strong class="slide-brand">{{ slideBrandName }}</strong>
              <div class="slide-content">
                <span class="slide-art-number" aria-hidden="true">{{ String(slideIndex + 1).padStart(2, '0') }}</span>
                <div class="slide-kicker">{{ slideArtifact?.title ?? slideTitle }} · {{ slideArtifact?.goal ?? slideAudience }}</div>
                <div class="slide-main"><h2>{{ activeSlideTitle }}</h2><p v-if="slideReady && slideEvidenceCurrent && activeSlideDraft" :class="{ 'ai-slide-copy': !!activeSlideDraft.ai_text }">{{ activeSlideDraft.ai_text || (activeSlideDraft.evidence_gap ? '待补充证据：本页没有匹配片段。' : activeSlideDraft.text) }}</p><p v-if="slideReady && slideEvidenceCurrent && activeSlideDraft?.manual_note" class="slide-manual-preview">人工补充（{{ activeSlideDraft.manual_note_by }}）：{{ activeSlideDraft.manual_note }}</p></div>
              </div>
              <div class="slide-footer"><span class="slide-page-number">{{ String(slideIndex + 1).padStart(2, '0') }} / {{ String(slideOutline.length).padStart(2, '0') }}</span></div>
            </div>
            <div v-if="activeSlideVisual && slidePreviewLayout !== 'focus'" class="slide-visual-credit"><span>{{ activeSlideVisual.source === 'document' ? `资料配图 · ${activeSlideVisual.source_title} ${activeSlideVisual.source_version} · ${activeSlideVisual.source_location}` : `主题配图 · ${activeSlideVisual.source === 'online' ? '在线图片' : '本地素材'}` }}</span><a v-if="activeSlideVisual.source === 'online' && activeSlideVisual.page_url" :href="activeSlideVisual.page_url" target="_blank" rel="noopener noreferrer">{{ activeSlideVisual.photographer }} / Unsplash</a></div>
            <label v-if="slideReady && slideArtifact?.status === 'pending'" class="field-label slide-note-field">本页标题<input v-model="slideOutline[slideIndex]" :aria-label="`第 ${slideIndex + 1} 页标题`" maxlength="100" /></label>
            <p v-if="slideReady && slideEvidenceCurrent && slideArtifact?.section_drafts[slideIndex]?.citation" class="slide-citation">来源：{{ slideArtifact.section_drafts[slideIndex].citation?.title }} {{ slideArtifact.section_drafts[slideIndex].citation?.version }} · {{ slideArtifact.section_drafts[slideIndex].citation?.location }}</p>
            <div v-if="slideReady && slideEvidenceCurrent && slideArtifact?.section_drafts[slideIndex]?.supporting_sources?.length && slideArtifact.section_drafts[slideIndex].supporting_sources!.length > 1" class="comparison-source slide-note-field"><strong>补充来源</strong><div v-for="source in slideArtifact.section_drafts[slideIndex].supporting_sources!.slice(1)" :key="source.citation.document_id"><p class="draft-excerpt">{{ source.text }}</p><p class="draft-citation">{{ source.citation.title }} {{ source.citation.version }} · {{ source.citation.location }}</p></div></div>
            <label v-if="slideReady && slideEvidenceCurrent && slideArtifact?.status === 'pending'" class="field-label slide-note-field">本页人工补充<textarea v-model="slideNoteEdits[slideIndex]" :aria-label="`第 ${slideIndex + 1} 页人工补充`" maxlength="160" rows="2" placeholder="补充汇报说明"></textarea></label>
            <p v-else-if="slideReady && slideEvidenceCurrent && slideArtifact?.section_drafts[slideIndex]?.manual_note" class="manual-note-readonly slide-note-field">人工补充（{{ slideArtifact.section_drafts[slideIndex].manual_note_by }}）：{{ slideArtifact.section_drafts[slideIndex].manual_note }}</p>
            <div v-if="slideReady && !slideEvidenceCurrent" class="evidence-gap slide-warning">提纲已修改，需更新证据后核对。</div>
            <div class="slide-controls"><button class="icon-btn bordered" :disabled="slideIndex === 0" aria-label="上一页" title="上一页" @click="slideIndex--"><ChevronLeft :size="18" /></button><span>{{ slideIndex + 1 }} / {{ slideOutline.length }}</span><button class="icon-btn bordered" :disabled="slideIndex === slideOutline.length - 1" aria-label="下一页" title="下一页" @click="slideIndex++"><ChevronRight :size="18" /></button><div class="spacer"></div><button v-if="slideReady && !slideEvidenceCurrent && slideArtifact?.status === 'pending'" class="button button-quiet" :disabled="workBusy" @click="refreshArtifactEvidence(slideArtifact, slideOutline)">更新证据</button><button v-if="slideArtifact?.status === 'pending' && slideEvidenceCurrent" class="button button-quiet" :disabled="workBusy || !slideNotesDirty" @click="saveNotes(slideArtifact)">{{ slideNotesDirty ? '保存人工补充' : '补充已保存' }}</button><button class="button button-quiet" :disabled="!slideReady || workBusy || (sessionState !== 'demo' && !slideEvidenceCurrent && slideArtifact?.status !== 'completed')" @click="exportArtifact(slideArtifact, slideOutline)"><ArrowDownToLine :size="16" />导出 PPTX</button></div>
            <div class="slide-list"><span>逐页提纲</span><button v-for="(item, index) in slideOutline" :key="index" :class="{ active: slideIndex === index }" @click="slideIndex = index"><small>{{ String(index + 1).padStart(2, '0') }}</small>{{ item }}</button></div>
          </div></div></section>

        <section v-else-if="currentView === 'tasks'" class="page standard-page">
          <div class="tabs"><button v-for="filter in ['全部', '已完成', '处理中', '待核对'] as const" :key="filter" :class="{ active: taskFilter === filter }" @click="taskFilter = filter">{{ filter }} <span>{{ filter === '全部' ? tasks.length : tasks.filter(task => task.status === filter).length }}</span></button></div>
          <div class="data-surface"><table><thead><tr><th>任务名称</th><th>类型</th><th>资料</th><th>更新时间</th><th>状态</th><th></th></tr></thead><tbody><tr v-for="task in pagedTasks" :key="task.id"><td><div class="file-cell"><span class="file-tile"><FileText :size="18" /></span><span><strong>{{ task.title }}</strong><small>{{ sessionState === 'demo' ? '界面演示' : '当前账号创建' }}</small></span></div></td><td>{{ task.type }}</td><td>{{ task.sources }} 份</td><td>{{ task.updated }}</td><td><span class="task-status" :class="task.status === '处理中' ? 'processing' : task.status === '待核对' ? 'review' : ''">{{ task.status }}</span></td><td><button class="link-button" @click="openTask(task)">查看 <ArrowRight :size="14" /></button></td></tr></tbody></table><div v-if="visibleTasks.length === 0" class="table-empty">当前没有任务</div><ListPager :page="taskPage" :total="visibleTasks.length" :page-size="taskPageSize" @change="taskPage = $event" /></div>
        </section>

        <section v-else class="page standard-page">
          <div class="notice"><ShieldCheck :size="20" /><div><strong>{{ sessionState === 'demo' ? '当前为界面演示' : '资料访问由服务端授权控制' }}</strong><span>{{ sessionState === 'demo' ? '演示数据仅用于预览界面。' : '上传资料经审核发布；问答和生成文件仅使用当前账号有权访问的资料。关键操作留有本地审计记录，向量检索尚未接入。' }}</span></div></div>
          <div class="governance-grid"><div class="governance-panel"><h2>资料可见范围</h2><div v-for="level in ['内部公开', '部门受限', '机密']" :key="level" class="governance-row"><span class="visibility-tag" :class="level === '机密' ? 'secret' : level === '部门受限' ? 'department' : ''">{{ level }}</span><span>{{ docs.filter(doc => doc.visibility === level).length }} 份可见资料</span></div></div><div class="governance-panel"><h2>审核与审计</h2><div class="governance-row"><span><FileCheck2 :size="18" />发布审核</span><small>{{ sessionState === 'demo' ? '演示模式' : '已接入本地审核' }}</small></div><div class="governance-row"><span><Clock3 :size="18" />授权变更</span><small>{{ sessionState === 'demo' ? '演示模式' : '关键操作已记录' }}</small></div><div class="governance-row"><span><LockKeyhole :size="18" />下载复核</span><small>{{ sessionState === 'demo' ? '演示模式' : '生成文件下载时复核来源权限' }}</small></div></div></div>
          <div v-if="currentUser?.role === 'system_admin'" class="audit-section">
            <div class="audit-heading"><h2>操作记录</h2><button class="button button-quiet" :disabled="auditBusy" @click="refreshAuditEvents">刷新</button></div>
            <div v-if="auditError" class="audit-empty" role="alert">{{ auditError }}</div>
            <div v-else-if="!auditBusy && auditEvents.length === 0" class="audit-empty">暂无操作记录</div>
            <div v-else class="audit-table"><table><thead><tr><th>时间</th><th>操作</th><th>操作者</th><th>资源 ID</th><th>结果</th><th>追踪 ID</th></tr></thead><tbody><tr v-for="event in auditEvents" :key="event.id"><td>{{ new Date(event.occurred_at).toLocaleString('zh-CN') }}</td><td>{{ auditActionLabels[event.action] ?? event.action }}</td><td>{{ event.actor_id }}</td><td>{{ event.resource_id || '—' }}</td><td><span :class="['audit-outcome', event.outcome]">{{ event.outcome === 'success' ? '成功' : '拒绝' }}</span></td><td><code>{{ event.trace_id.slice(0, 12) }}</code></td></tr></tbody></table></div>
          </div>
        </section>
      </main>
    </div>

    <div v-if="activeClarification && sessionState === 'ready'" class="modal-overlay" @click.self="closeClarification">
      <div class="modal clarification-modal" role="dialog" aria-modal="true" aria-labelledby="clarification-title">
        <div class="modal-top"><h2 id="clarification-title">补充一个条件</h2><button class="icon-btn" type="button" aria-label="关闭" @click="closeClarification"><X :size="18" /></button></div>
        <form class="modal-body" @submit.prevent="submitClarification">
          <p class="clarification-prompt">{{ activeClarification.prompt }}</p>
          <div v-if="activeClarification.options.length" class="clarification-options" role="group" :aria-label="activeClarification.field">
            <button v-for="option in activeClarification.options" :key="option" type="button" :class="{ selected: clarificationValue === option }" :aria-pressed="clarificationValue === option" @click="clarificationValue = option">{{ option }}{{ activeClarification.unit }}</button>
          </div>
          <label v-if="activeClarification.input_type === 'number'" class="clarification-input">{{ activeClarification.field }}<span><input v-model="clarificationValue" type="number" min="0" max="100" step="any" required :aria-label="activeClarification.field" /><em>{{ activeClarification.unit }}</em></span></label>
          <div class="modal-actions"><button class="button button-quiet" type="button" @click="closeClarification">稍后填写</button><button class="button button-primary" type="submit" :disabled="!clarificationValue || questionBusy">继续回答</button></div>
        </form>
      </div>
    </div>
    <div v-if="conversationToDelete && sessionState === 'ready'" class="modal-overlay" @click.self="!conversationDeleteBusy && (conversationToDelete = null)">
      <div class="modal conversation-delete-modal" role="dialog" aria-modal="true" aria-labelledby="conversation-delete-title" aria-describedby="conversation-delete-description">
        <div class="modal-top"><h2 id="conversation-delete-title">删除会话</h2><button class="icon-btn" type="button" aria-label="关闭" :disabled="conversationDeleteBusy" @click="conversationToDelete = null"><X :size="19" /></button></div>
        <div class="modal-body"><p id="conversation-delete-description">确定删除“{{ conversationToDelete.title }}”吗？会话消息及从该会话自动提取的长期记忆会被移除，此操作无法撤销。</p><div class="modal-actions"><button class="button button-quiet" type="button" :disabled="conversationDeleteBusy" @click="conversationToDelete = null">取消</button><button class="button button-danger" type="button" :disabled="conversationDeleteBusy" @click="confirmDeleteConversation">{{ conversationDeleteBusy ? '正在删除…' : '删除会话' }}</button></div></div>
      </div>
    </div>
    <div v-if="editPreviewTarget" class="modal-overlay" @click.self="closeEditPreview">
      <div class="modal edit-preview-modal" role="dialog" aria-modal="true" aria-labelledby="edit-preview-title">
        <div class="modal-top"><h2 id="edit-preview-title">文件预览</h2><button class="icon-btn" type="button" aria-label="关闭预览" title="关闭预览" @click="closeEditPreview"><X :size="19" /></button></div>
        <div class="modal-body edit-preview-body">
          <p v-if="editPreviewBusy" class="edit-preview-state"><LoaderCircle class="answer-wait-icon" :size="17" />正在加载预览</p>
          <p v-else-if="editPreviewError" class="edit-preview-state error">{{ editPreviewError }}</p>
          <template v-else-if="editPreviewArtifact">
            <div class="edit-preview-heading"><FileText :size="20" /><div><strong>{{ editPreviewArtifact.title }}</strong><span>{{ editPreviewArtifact.kind.toUpperCase() }} · 待确认草稿 · {{ editPreviewArtifact.section_drafts.length }} 个章节</span></div></div>
            <div class="edit-preview-meta"><span>任务要求</span><p>{{ editPreviewArtifact.goal || '未填写' }}</p><span>来源</span><p>{{ editPreviewArtifact.source_ids.map(id => docs.find(doc => String(doc.id) === id)?.title || id).join('、') }}</p></div>
            <div v-for="(section, index) in editPreviewArtifact.section_drafts" :key="index" class="edit-preview-section"><h3>{{ section.heading }}</h3><small>{{ section.ai_text ? 'AI 草稿' : '来源摘录' }}</small><p>{{ section.ai_text || section.text || '暂无正文内容' }}</p><template v-if="section.ai_text"><small>依据摘录</small><p class="edit-preview-evidence">{{ section.text }}</p></template><p v-if="section.manual_note" class="edit-preview-note">人工补充：{{ section.manual_note }}</p><small v-if="section.citation">{{ section.citation.title }} · {{ section.citation.version }} · {{ section.citation.location }}</small></div>
          </template>
          <template v-else-if="editPreviewDocument">
            <div class="edit-preview-heading"><FileText :size="20" /><div><strong>{{ editPreviewDocument.title }}</strong><span>{{ editPreviewDocument.type }} · {{ editPreviewDocument.area }} · {{ editPreviewDocument.version }}</span></div></div>
            <div class="edit-preview-meta"><span>可见范围</span><p>{{ editPreviewDocument.visibility }}</p><span>负责人</span><p>{{ editPreviewDocument.owner }}</p><span>定位</span><p>{{ editPreviewDocument.page }}</p></div>
            <div class="edit-preview-section"><h3>正文预览</h3><p class="edit-preview-text">{{ editPreviewDocument.preview_text }}</p><small v-if="editPreviewDocument.truncated">仅显示前 6000 字</small></div>
          </template>
        </div>
        <div class="modal-actions edit-preview-actions"><button class="button button-quiet" type="button" @click="closeEditPreview">关闭</button><button class="button button-primary" type="button" :disabled="editPreviewBusy || !!editPreviewError" @click="choosePreviewedFile">{{ editPreviewSelected ? '取消选择' : '选择此文件' }}</button></div>
      </div>
    </div>
    <div v-if="selectedDoc" class="modal-overlay" @click.self="selectedDoc = null">
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="document-dialog-title">
        <div class="modal-top"><h2 id="document-dialog-title">资料详情</h2><button class="icon-btn" aria-label="关闭" title="关闭" @click="selectedDoc = null"><X :size="19" /></button></div>
        <div class="modal-body">
          <div class="detail-heading"><span class="file-tile"><FileText :size="20" /></span><div><strong>{{ selectedDoc.title }}</strong><span>{{ selectedDoc.type }} · {{ selectedDoc.area }}</span></div></div>
          <div class="detail-grid"><div><span>可见范围</span><strong>{{ selectedDoc.visibility }}</strong></div><div><span>版本</span><strong>{{ selectedDoc.version }}</strong></div><div><span>负责人</span><strong>{{ selectedDoc.owner }}</strong></div><div><span>定位</span><strong>{{ selectedDoc.page }}</strong></div></div>
          <h3>资料片段</h3><blockquote>{{ selectedDoc.excerpt }}</blockquote>
          <p v-if="!selectedDoc.has_original" class="original-unavailable">{{ sessionState === 'demo' ? '界面演示资料不提供原件下载。' : '这份资料暂无可下载原件，可继续查看片段或针对资料提问。' }}</p>
          <div v-if="currentUser && selectedDoc.owner === currentUser.user_id && (currentUser.role === 'department_manager' || currentUser.role === 'knowledge_admin')" class="version-upload"><label class="field-label">提交新版本<input type="file" accept=".pdf,.docx,.pptx,.txt,.md" @change="versionFile = ($event.target as HTMLInputElement).files?.[0] ?? null" /></label><button class="button button-quiet" type="button" :disabled="!versionFile || versionBusy" @click="submitVersion">{{ versionBusy ? '正在提交' : '提交审核' }}</button></div>
          <div class="modal-actions"><button class="button button-quiet" @click="selectedDoc = null">关闭</button><button class="button button-quiet" :disabled="!selectedDoc.has_original || originalBusy" :title="selectedDoc.has_original ? '下载当前版本原件' : '这份资料没有上传原件'" @click="downloadOriginal"><ArrowDownToLine :size="16" />{{ originalBusy ? '下载中' : '下载原件' }}</button><button class="button button-primary" @click="askAbout(selectedDoc)"><MessageSquareText :size="16" />针对资料提问</button></div>
        </div>
      </div>
    </div>
    <div v-if="uploadOpen" class="modal-overlay" @click.self="uploadOpen = false"><div class="modal" role="dialog" aria-modal="true" aria-labelledby="upload-dialog-title"><div class="modal-top"><h2 id="upload-dialog-title">上传资料</h2><button class="icon-btn" aria-label="关闭" title="关闭" @click="uploadOpen = false"><X :size="19" /></button></div><form class="modal-body" @submit.prevent="submitUpload"><label class="upload-area"><FilePlus2 :size="24" /><strong>{{ selectedFile?.name ?? '选择本地文件' }}</strong><span>支持 PDF、DOCX、PPTX、TXT、MD，上传后审核发布</span><input type="file" accept=".pdf,.docx,.pptx,.txt,.md" @change="handleFile" /></label><label class="field-label">资料名称<input v-model="uploadName" required /></label><div class="two-fields"><label class="field-label">知识空间<select v-model="uploadArea"><option>制度规范</option><option>产品资料</option><option>项目空间</option></select></label><label class="field-label">可见范围<select v-model="uploadVisibility"><option>内部公开</option><option>部门受限</option><option>机密</option></select></label></div><div class="modal-actions"><button type="button" class="button button-quiet" @click="uploadOpen = false">取消</button><button type="submit" class="button button-primary">提交审核</button></div></form></div></div>
    <div v-if="toast" class="toast" role="status">{{ toast }}</div>
  </div>
</template>
