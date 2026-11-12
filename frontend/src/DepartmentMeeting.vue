<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ArrowUp, ChevronLeft, FileText, LoaderCircle, MessageSquareText, Plus, Search, Users, UserPlus, X } from 'lucide-vue-next'
import { addMeetingMember, createMeetingRoom, getMeetingRoom, listMeetingCandidates,
  listMeetingMessages, listMeetingRooms, postMeetingMessage, removeMeetingMember, draftMeetingMinutes,
  type CurrentUser, type MeetingCandidate, type MeetingMessage, type MeetingMinutes, type MeetingRoom } from './auth'

const props = defineProps<{ user: CurrentUser | null }>()
const rooms = ref<MeetingRoom[]>([])
const activeId = ref<string | null>(null)
const room = ref<MeetingRoom | null>(null)
const messages = ref<MeetingMessage[]>([])
const candidates = ref<MeetingCandidate[]>([])
const loading = ref(false)
const sending = ref(false)
const managing = ref(false)
const error = ref('')
const draft = ref('')
const roomSearch = ref('')
const memberSearch = ref('')
const membersOpen = ref(false)
const createOpen = ref(false)
const createTitle = ref('')
const createDepartment = ref('')
const messageList = ref<HTMLElement | null>(null)
const hasOlder = ref(false)
const minutes = ref<MeetingMinutes | null>(null)
const minutesOpen = ref(false)
const minutesLoading = ref(false)
const minutesError = ref('')
const minuteGroups = [
  { key: 'discussion', title: '讨论要点' },
  { key: 'decisions', title: '明确决议' },
  { key: 'actions', title: '待办事项' }
] as const
let timer: ReturnType<typeof setInterval> | undefined
let selection = 0
let polling = false

const canCreate = computed(() => !!props.user?.department_ids.length &&
  ['department_manager', 'knowledge_admin', 'system_admin'].includes(props.user.role))
const isOwner = computed(() => !!room.value && room.value.owner_id === props.user?.user_id)
const filteredRooms = computed(() => rooms.value.filter(item =>
  `${item.title} ${item.department_id}`.toLocaleLowerCase().includes(roomSearch.value.trim().toLocaleLowerCase())))
const availableCandidates = computed(() => candidates.value.filter(item =>
  !room.value?.members?.some(member => member.user_id === item.user_id) &&
  `${item.username} ${item.user_id} ${item.department_ids.join(' ')}`.toLocaleLowerCase()
    .includes(memberSearch.value.trim().toLocaleLowerCase())))
const names = computed(() => Object.fromEntries((room.value?.members ?? []).map(item => [item.user_id, item.username])))

function timeLabel(value: string): string {
  return new Date(value).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

async function scrollToBottom() {
  await nextTick()
  if (messageList.value) messageList.value.scrollTop = messageList.value.scrollHeight
}

async function selectRoom(id: string) {
  const request = ++selection
  activeId.value = id
  room.value = null
  messages.value = []
  membersOpen.value = false
  minutesOpen.value = false
  minutes.value = null
  minutesError.value = ''
  loading.value = true
  error.value = ''
  try {
    const [detail, history] = await Promise.all([getMeetingRoom(id), listMeetingMessages(id)])
    if (request !== selection) return
    room.value = detail
    messages.value = history
    hasOlder.value = history.length === 100
    await scrollToBottom()
  } catch (cause) {
    if (request === selection) {
      activeId.value = null
      error.value = cause instanceof Error ? cause.message : '会议室加载失败'
      void refreshRooms()
    }
  } finally {
    if (request === selection) loading.value = false
  }
}

async function refreshRooms() {
  if (!props.user) return
  try {
    const items = await listMeetingRooms()
    rooms.value = items
    if (activeId.value && !items.some(item => item.id === activeId.value)) {
      activeId.value = null
      room.value = null
      messages.value = []
    }
    if (!activeId.value && items.length) await selectRoom(items[0].id)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '会议室列表加载失败'
  }
}

async function generateMinutes() {
  const id = activeId.value
  if (!id || minutesLoading.value) return
  minutesOpen.value = true
  minutesLoading.value = true
  minutes.value = null
  minutesError.value = ''
  error.value = ''
  try {
    const result = await draftMeetingMinutes(id)
    if (activeId.value === id) minutes.value = result
  } catch (cause) {
    if (activeId.value === id) minutesError.value = cause instanceof Error ? cause.message : '生成纪要失败'
  } finally { minutesLoading.value = false }
}

async function jumpToMessage(id: number) {
  minutesOpen.value = false
  await nextTick()
  messageList.value?.querySelector(`[data-message-id="${id}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

async function poll() {
  if (polling || !props.user) return
  polling = true
  try {
    const items = await listMeetingRooms()
    rooms.value = items
    const id = activeId.value
    if (!id) {
      if (items.length) await selectRoom(items[0].id)
      return
    }
    if (!items.some(item => item.id === id)) {
      activeId.value = null
      room.value = null
      messages.value = []
      if (items.length) await selectRoom(items[0].id)
      return
    }
    const lastId = messages.value.at(-1)?.id ?? 0
    const [detail, additions] = await Promise.all([getMeetingRoom(id), listMeetingMessages(id, lastId)])
    if (activeId.value !== id) return
    room.value = detail
    if (additions.length) {
      const nearBottom = !messageList.value ||
        messageList.value.scrollHeight - messageList.value.scrollTop - messageList.value.clientHeight < 90
      const seen = new Set(messages.value.map(item => item.id))
      messages.value.push(...additions.filter(item => !seen.has(item.id)))
      if (nearBottom) await scrollToBottom()
    }
  } catch {
    // The next poll retries; send and invite errors remain visible to the user.
  } finally { polling = false }
}

async function send() {
  const text = draft.value.trim()
  const id = activeId.value
  if (!text || !id || sending.value) return
  sending.value = true
  error.value = ''
  try {
    const message = await postMeetingMessage(id, text)
    if (activeId.value === id) {
      draft.value = ''
      if (!messages.value.some(item => item.id === message.id)) messages.value.push(message)
      await scrollToBottom()
    }
    void refreshRooms()
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '发送失败' }
  finally { sending.value = false }
}

function onDraftKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    void send()
  }
}

async function older() {
  const id = activeId.value
  const first = messages.value[0]?.id
  if (!id || !first || loading.value) return
  loading.value = true
  try {
    const oldHeight = messageList.value?.scrollHeight ?? 0
    const rows = await listMeetingMessages(id, 0, first)
    if (activeId.value !== id) return
    hasOlder.value = rows.length === 100
    messages.value.unshift(...rows)
    await nextTick()
    if (messageList.value) messageList.value.scrollTop += messageList.value.scrollHeight - oldHeight
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '较早消息加载失败' }
  finally { loading.value = false }
}

async function openMembers() {
  membersOpen.value = true
  if (!isOwner.value) return
  try { candidates.value = await listMeetingCandidates() }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '成员目录加载失败' }
}

async function invite(userId: string) {
  const id = activeId.value
  if (!id || managing.value) return
  managing.value = true
  error.value = ''
  try {
    room.value = await addMeetingMember(id, userId)
    await refreshRooms()
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '邀请失败' }
  finally { managing.value = false }
}

async function remove(userId: string) {
  const id = activeId.value
  if (!id || managing.value) return
  managing.value = true
  error.value = ''
  try {
    await removeMeetingMember(id, userId)
    room.value = await getMeetingRoom(id)
    await refreshRooms()
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '移出成员失败' }
  finally { managing.value = false }
}

function openCreate() {
  createDepartment.value = props.user?.department_ids[0] ?? ''
  createTitle.value = `${createDepartment.value} 部门会议室`
  createOpen.value = true
}

async function create() {
  if (!createDepartment.value || !createTitle.value.trim() || managing.value) return
  managing.value = true
  error.value = ''
  try {
    const created = await createMeetingRoom(createDepartment.value, createTitle.value.trim())
    createOpen.value = false
    await refreshRooms()
    await selectRoom(created.id)
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '创建会议室失败' }
  finally { managing.value = false }
}

watch(() => props.user?.user_id, () => {
  selection++
  rooms.value = []
  activeId.value = null
  room.value = null
  messages.value = []
  minutes.value = null
  minutesOpen.value = false
  minutesError.value = ''
  if (props.user) void refreshRooms()
}, { immediate: true })
onMounted(() => { timer = setInterval(() => { void poll() }, 3000) })
onUnmounted(() => { selection++; if (timer) clearInterval(timer) })
</script>

<template>
  <section class="meeting-page" aria-label="部门会议室">
    <aside class="meeting-rooms">
      <div class="meeting-rooms-head"><div><strong>部门会议室</strong><small>{{ rooms.length }} 个空间</small></div><button v-if="canCreate" type="button" title="创建会议室" aria-label="创建会议室" @click="openCreate"><Plus :size="18" /></button></div>
      <label v-if="rooms.length" class="meeting-search"><Search :size="16" /><input v-model="roomSearch" type="search" placeholder="查找会议室" aria-label="查找会议室" /></label>
      <div class="meeting-room-list"><button v-for="item in filteredRooms" :key="item.id" type="button" class="meeting-room-item" :class="{ active: activeId === item.id }" @click="selectRoom(item.id)"><span class="meeting-room-icon"><Users :size="18" /></span><span class="meeting-room-copy"><strong>{{ item.title }}</strong><small>{{ item.last_message || `${item.department_id} · ${item.member_count ?? 1} 人` }}</small></span><span class="meeting-room-time">{{ item.last_message ? timeLabel(item.updated_at || item.created_at) : '' }}</span></button></div>
      <div v-if="!rooms.length" class="meeting-list-empty"><MessageSquareText :size="24" /><span>{{ user ? '还没有加入会议室' : '登录后查看会议室' }}</span></div>
    </aside>

    <div class="meeting-main">
      <div v-if="room" class="meeting-chat">
        <header class="meeting-chat-head"><div class="meeting-chat-title"><span class="meeting-room-icon"><Users :size="19" /></span><div><h1>{{ room.title }}</h1><small>{{ room.department_id }} · {{ room.members?.length ?? 1 }} 位成员</small></div></div><div class="meeting-head-actions"><button class="meeting-members-button" type="button" :disabled="!messages.length || minutesLoading" @click="generateMinutes"><LoaderCircle v-if="minutesLoading" :size="16" class="meeting-spin" /><FileText v-else :size="16" />生成纪要</button><button class="meeting-members-button" type="button" @click="openMembers"><Users :size="17" />成员</button></div></header>
        <div ref="messageList" class="meeting-messages" aria-live="polite"><button v-if="hasOlder" class="meeting-older" type="button" :disabled="loading" @click="older"><ChevronLeft :size="15" />查看较早消息</button><div v-if="!messages.length && !loading" class="meeting-chat-empty"><MessageSquareText :size="28" /><strong>暂无消息</strong><span>发送第一条消息</span></div><div v-for="message in messages" :key="message.id" class="meeting-message" :data-message-id="message.id" :class="{ mine: message.sender_id === user?.user_id }"><span class="meeting-avatar">{{ (names[message.sender_id] || message.sender_id).slice(0, 1).toUpperCase() }}</span><div class="meeting-message-body"><div class="meeting-message-meta"><strong>{{ names[message.sender_id] || message.sender_id }}</strong><time>{{ timeLabel(message.created_at) }}</time></div><p>{{ message.text }}</p></div></div></div>
        <form class="meeting-composer" @submit.prevent="send"><textarea v-model="draft" rows="2" maxlength="2000" :disabled="sending" placeholder="发送消息到会议室" aria-label="会议室消息" @keydown="onDraftKeydown"></textarea><div><span>{{ draft.length }}/2000</span><button type="submit" :disabled="!draft.trim() || sending" title="发送消息" aria-label="发送消息"><LoaderCircle v-if="sending" :size="17" class="meeting-spin" /><ArrowUp v-else :size="18" /></button></div></form>
      </div>
      <div v-else class="meeting-welcome"><span class="meeting-welcome-icon"><Users :size="29" /></span><h1>{{ loading ? '正在打开会议室' : '部门会议室' }}</h1><p>{{ user ? rooms.length ? '选择一个会议室开始交流' : canCreate ? '创建本部门会议室，邀请成员参与讨论。' : '等待会议室负责人邀请。' : '登录后参与部门讨论。' }}</p><button v-if="canCreate && !rooms.length" class="meeting-primary" type="button" @click="openCreate"><Plus :size="16" />创建会议室</button></div>
      <div v-if="error" class="meeting-error" role="alert">{{ error }}<button type="button" aria-label="关闭提示" @click="error = ''"><X :size="14" /></button></div>
    </div>

    <aside v-if="membersOpen && room" class="meeting-members"><div class="meeting-members-head"><div><strong>成员</strong><small>{{ room.members?.length ?? 0 }} 人</small></div><button type="button" title="关闭成员列表" aria-label="关闭成员列表" @click="membersOpen = false"><X :size="18" /></button></div><div class="meeting-member-list"><div v-for="member in room.members" :key="member.user_id" class="meeting-member"><span class="meeting-avatar">{{ member.username.slice(0, 1).toUpperCase() }}</span><div><strong>{{ member.username }}</strong><small>{{ member.user_id === room.owner_id ? '负责人' : member.user_id }}</small></div><button v-if="isOwner && member.user_id !== room.owner_id" type="button" :disabled="managing" :title="`移出 ${member.username}`" :aria-label="`移出 ${member.username}`" @click="remove(member.user_id)"><X :size="15" /></button></div></div><div v-if="isOwner" class="meeting-invite"><h2><UserPlus :size="16" />邀请成员</h2><label class="meeting-search"><Search :size="15" /><input v-model="memberSearch" type="search" placeholder="搜索账号或部门" aria-label="搜索可邀请账号" /></label><div class="meeting-candidates"><div v-for="candidate in availableCandidates" :key="candidate.user_id" class="meeting-candidate"><span><strong>{{ candidate.username }}</strong><small>{{ candidate.department_ids.join('、') }}</small></span><button type="button" :disabled="managing" :title="`邀请 ${candidate.username}`" :aria-label="`邀请 ${candidate.username}`" @click="invite(candidate.user_id)"><Plus :size="16" /></button></div><p v-if="!availableCandidates.length">没有可邀请的账号</p></div></div></aside>

    <div v-if="createOpen" class="meeting-modal-overlay" @click.self="createOpen = false"><form class="meeting-modal" @submit.prevent="create"><div class="meeting-modal-head"><h2>创建部门会议室</h2><button type="button" title="关闭" aria-label="关闭" @click="createOpen = false"><X :size="18" /></button></div><label>所属部门<select v-model="createDepartment"><option v-for="department in user?.department_ids ?? []" :key="department" :value="department">{{ department }}</option></select></label><label>会议室名称<input v-model="createTitle" maxlength="80" required minlength="2" /></label><div class="meeting-modal-actions"><button type="button" @click="createOpen = false">取消</button><button class="meeting-primary" type="submit" :disabled="managing || !createTitle.trim()">创建会议室</button></div></form></div>
    <div v-if="minutesOpen" class="meeting-modal-overlay" @click.self="minutesOpen = false">
      <section class="meeting-minutes" role="dialog" aria-modal="true" aria-label="会议纪要草稿">
        <div class="meeting-modal-head"><div><h2>会议纪要草稿</h2><small v-if="minutes">依据最近 {{ minutes.source_count }} 条消息 · #{{ minutes.first_message_id }}–#{{ minutes.last_message_id }}</small></div><button type="button" title="关闭" aria-label="关闭纪要" @click="minutesOpen = false"><X :size="18" /></button></div>
        <p class="meeting-minutes-note">以下为按聊天原文摘录的 AI 分类草稿。点击条目可回到对应消息，决议与待办请由参会人确认。</p>
        <div v-if="minutesLoading" class="meeting-minutes-wait"><LoaderCircle :size="18" class="meeting-spin" />正在整理消息…</div>
        <div v-else-if="minutesError" class="meeting-minutes-fail" role="alert">{{ minutesError }}<button type="button" @click="generateMinutes">重试</button></div>
        <template v-else-if="minutes">
          <p v-if="minutes.truncated" class="meeting-minutes-note">消息较多，仅整理最近一段对话。</p>
          <section v-for="group in minuteGroups" :key="group.key" class="meeting-minutes-group">
            <h3>{{ group.title }}</h3><p v-if="!minutes[group.key].length" class="meeting-minutes-empty">聊天中尚无明确记录</p>
            <button v-for="item in minutes[group.key]" :key="item.message_id" type="button" class="meeting-minutes-item" @click="jumpToMessage(item.message_id)"><span>{{ item.quote }}</span><small>消息 #{{ item.message_id }} · {{ names[item.sender_id] || item.sender_id }} · {{ timeLabel(item.created_at) }}</small></button>
          </section>
        </template>
      </section>
    </div>
  </section>
</template>

<style scoped>
.meeting-page{position:relative;height:calc(100dvh - 61px);min-height:0;display:grid;grid-template-columns:260px minmax(0,1fr);grid-template-rows:minmax(0,1fr);background:#fff;color:#263d31}
.meeting-rooms{min-width:0;border-right:1px solid #e3ebe6;background:#f7faf8;display:flex;flex-direction:column}
.meeting-rooms-head,.meeting-members-head{min-height:76px;padding:20px 18px 12px;display:flex;align-items:center;justify-content:space-between;gap:10px}
.meeting-rooms-head>div,.meeting-members-head>div{display:flex;flex-direction:column;gap:2px}.meeting-rooms-head strong,.meeting-members-head strong{font-size:14px}.meeting-rooms-head small,.meeting-members-head small{font-size:11px;color:#8b9c91}
.meeting-rooms-head button,.meeting-members-head button,.meeting-modal-head button{height:30px;width:30px;border:0;border-radius:5px;background:transparent;color:#6d8374;display:grid;place-items:center}.meeting-rooms-head button:hover,.meeting-members-head button:hover,.meeting-modal-head button:hover{background:#e5efe9;color:#087965}
.meeting-search{height:36px;margin:4px 13px 12px;padding:0 10px;display:flex;align-items:center;gap:8px;border:1px solid #e0e9e2;background:#fff;border-radius:5px;color:#879c8d}.meeting-search input{width:100%;min-width:0;border:0;background:transparent;font-size:12px;color:#31483a}.meeting-search input:focus{outline:none}
.meeting-room-list{overflow-y:auto;min-height:0;padding:0 8px;flex:1}.meeting-room-item{width:100%;min-height:63px;display:flex;align-items:center;gap:10px;padding:9px;border:0;border-radius:5px;background:transparent;text-align:left;color:inherit}.meeting-room-item:hover{background:#edf4ef}.meeting-room-item.active{background:#e0efe6}.meeting-room-icon{width:35px;height:35px;display:grid;place-items:center;flex:none;border-radius:6px;background:#dcebe2;color:#26785f}.meeting-room-item.active .meeting-room-icon{background:#c9e5d5;color:#087965}.meeting-room-copy{display:flex;flex-direction:column;gap:3px;min-width:0;flex:1}.meeting-room-copy strong{font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.meeting-room-copy small{font-size:10px;color:#8a9d8e;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.meeting-room-time{align-self:flex-start;color:#9aab9f;font-size:10px;white-space:nowrap}.meeting-list-empty{flex:1;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:12px;color:#91a499;font-size:12px}
.meeting-main{min-width:0;min-height:0;position:relative}.meeting-chat{height:100%;min-height:0;display:flex;flex-direction:column}.meeting-chat-head{height:72px;flex:none;padding:0 25px;border-bottom:1px solid #e9efeb;display:flex;align-items:center;justify-content:space-between;gap:15px}.meeting-chat-title{display:flex;align-items:center;gap:11px;min-width:0}.meeting-chat-title h1{font-size:15px;margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.meeting-chat-title small{font-size:11px;color:#91a398}.meeting-members-button{height:34px;display:flex;align-items:center;gap:7px;border:1px solid #dce8df;border-radius:5px;background:#fff;color:#4c6957;padding:0 11px;font-size:11px;font-weight:650}.meeting-members-button:hover{background:#f0f7f2}
.meeting-messages{flex:1;min-height:0;overflow-y:auto;padding:25px max(26px,calc((100% - 760px)/2)) 16px}.meeting-older{display:flex;align-items:center;gap:4px;margin:0 auto 23px;border:0;background:transparent;color:#378068;font-size:11px}.meeting-older svg{transform:rotate(90deg)}.meeting-chat-empty{height:100%;min-height:150px;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:7px;color:#8ba698}.meeting-chat-empty strong{font-size:13px;color:#566f5f}.meeting-chat-empty span{font-size:11px}.meeting-message{display:flex;align-items:flex-start;gap:9px;margin:0 0 19px;max-width:78%}.meeting-avatar{width:29px;height:29px;border-radius:6px;flex:none;display:grid;place-items:center;background:#e2ebe5;color:#4e755c;font-size:12px;font-weight:700}.meeting-message-body{min-width:0}.meeting-message-meta{display:flex;align-items:center;gap:9px;margin:0 0 5px}.meeting-message-meta strong{font-size:11px}.meeting-message-meta time{font-size:10px;color:#a1aea4}.meeting-message p{margin:0;width:fit-content;max-width:100%;padding:9px 12px;border-radius:3px 7px 7px 7px;background:#f1f5f2;color:#304638;font-size:12px;line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere}.meeting-message.mine{margin-left:auto;flex-direction:row-reverse}.meeting-message.mine .meeting-message-meta{justify-content:flex-end}.meeting-message.mine p{margin-left:auto;border-radius:7px 3px 7px 7px;background:#e3f2e9}.meeting-message.mine .meeting-avatar{background:#cfebda;color:#138064}
.meeting-composer{flex:none;margin:10px max(22px,calc((100% - 780px)/2)) 18px;border:1px solid #dce8df;border-radius:7px;background:#fff}.meeting-composer:focus-within{border-color:#96b9a4}.meeting-composer textarea{display:block;width:100%;min-height:62px;max-height:140px;resize:vertical;padding:12px 13px 0;border:0;background:transparent;color:#2d4837;font-size:12px}.meeting-composer textarea:focus{outline:none}.meeting-composer>div{display:flex;align-items:center;justify-content:space-between;padding:0 8px 8px 13px}.meeting-composer span{font-size:10px;color:#a1afa5}.meeting-composer button{width:31px;height:31px;border:0;border-radius:5px;background:#087e6d;color:white;display:grid;place-items:center}.meeting-composer button:hover{background:#066c5e}.meeting-spin{animation:meeting-spin 1s linear infinite}@keyframes meeting-spin{to{transform:rotate(360deg)}}
.meeting-welcome{height:100%;display:flex;align-items:center;justify-content:center;flex-direction:column;text-align:center;padding:30px}.meeting-welcome-icon{width:54px;height:54px;border-radius:8px;display:grid;place-items:center;background:#e7f2eb;color:#16846a}.meeting-welcome h1{font-size:17px;margin:16px 0 3px}.meeting-welcome p{font-size:12px;color:#829588;margin:0 0 22px;line-height:1.6}.meeting-primary{min-height:35px;border:0;border-radius:5px;background:#087e6d;color:#fff;font-size:11px;font-weight:700;display:inline-flex;align-items:center;justify-content:center;gap:7px;padding:0 13px}.meeting-primary:hover{background:#086b5e}.meeting-error{position:absolute;left:22px;right:22px;bottom:111px;padding:9px 10px;border:1px solid #efcfca;border-radius:5px;background:#fff8f6;color:#9b4c42;font-size:11px;display:flex;align-items:center;justify-content:space-between;gap:10px}.meeting-error button{border:0;background:transparent;color:inherit;display:grid;place-items:center}
.meeting-members{position:absolute;top:0;right:0;bottom:0;z-index:12;width:min(360px,100%);border-left:1px solid #e3ebe6;background:#fbfdfb;box-shadow:-8px 0 24px #19332216;display:flex;flex-direction:column;min-height:0}.meeting-member-list{padding:0 12px;overflow-y:auto;max-height:40%;flex:none}.meeting-member{display:flex;align-items:center;gap:9px;min-height:48px;padding:6px}.meeting-member>div{display:flex;flex-direction:column;gap:1px;min-width:0;flex:1}.meeting-member strong{font-size:11px;overflow:hidden;text-overflow:ellipsis}.meeting-member small{font-size:10px;color:#9aac9d;overflow:hidden;text-overflow:ellipsis}.meeting-member button,.meeting-candidate button{border:0;background:transparent;color:#8a9e8e;width:28px;height:28px;display:grid;place-items:center;border-radius:5px}.meeting-member button:hover{background:#f9e9e6;color:#b05d4c}.meeting-invite{flex:1;min-height:0;display:flex;flex-direction:column;border-top:1px solid #e5ede6;margin-top:10px;padding-top:12px}.meeting-invite h2{display:flex;align-items:center;gap:7px;margin:0 18px 10px;font-size:11px;color:#4f6c59}.meeting-invite .meeting-search{margin-bottom:5px;flex:none}.meeting-candidates{flex:1;overflow-y:auto;min-height:0;padding:3px 12px 16px}.meeting-candidate{min-height:42px;display:flex;align-items:center;justify-content:space-between;padding:5px 7px;border-radius:5px}.meeting-candidate:hover{background:#eef5ef}.meeting-candidate span{display:flex;flex-direction:column;gap:1px}.meeting-candidate strong{font-size:11px}.meeting-candidate small,.meeting-candidates p{font-size:10px;color:#91a397}.meeting-candidate button:hover{background:#dbeee1;color:#087965}
.meeting-modal-overlay{position:fixed;inset:0;z-index:50;background:#17282080;display:grid;place-items:center;padding:16px}.meeting-modal{width:min(420px,100%);background:#fff;border-radius:7px;padding:22px;box-shadow:0 20px 60px #14261b2b}.meeting-modal-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}.meeting-modal h2{font-size:15px;margin:0}.meeting-modal label{display:flex;flex-direction:column;gap:6px;margin-top:13px;font-size:11px;color:#617969}.meeting-modal input,.meeting-modal select{height:38px;border:1px solid #d9e5dd;border-radius:5px;background:#fff;color:#304738;padding:0 10px;font-size:12px}.meeting-modal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:22px}.meeting-modal-actions>button:first-child{border:1px solid #d9e5dd;background:#fff;border-radius:5px;color:#4f6b58;padding:0 13px;font-size:11px}
@media(min-width:761px){.meeting-page:has(.meeting-members){grid-template-columns:clamp(200px,20vw,260px) minmax(0,1fr) clamp(280px,28vw,360px)}.meeting-members{position:relative;width:auto;box-shadow:none}}
@media(max-width:760px){.meeting-page{height:calc(100dvh - 54px);display:flex;flex-direction:column}.meeting-rooms{width:100%;height:116px;flex:none;border-right:0;border-bottom:1px solid #e3ebe6}.meeting-rooms-head{min-height:39px;padding:6px 15px}.meeting-rooms-head strong{font-size:12px}.meeting-rooms-head small{display:none}.meeting-rooms .meeting-search{display:none}.meeting-room-list{display:flex;gap:5px;overflow-x:auto;overflow-y:hidden;padding:3px 10px 8px}.meeting-room-item{min-width:175px;max-width:220px;min-height:58px}.meeting-room-time{display:none}.meeting-list-empty{font-size:11px;flex-direction:row}.meeting-main{flex:1}.meeting-chat-head{height:60px;padding:0 15px}.meeting-messages{padding:18px 16px}.meeting-composer{margin:8px 12px 12px}.meeting-members{width:min(360px,100%)}.meeting-message{max-width:92%}}
.meeting-head-actions{display:flex;gap:8px;flex:none}.meeting-members-button:disabled{opacity:.5;cursor:not-allowed}
.meeting-minutes{width:min(690px,100%);max-height:min(80dvh,820px);overflow:auto;background:#fff;border-radius:10px;padding:24px;box-shadow:0 20px 60px #14261b2b}
.meeting-minutes .meeting-modal-head{margin-bottom:8px}.meeting-minutes .meeting-modal-head>div{display:flex;flex-direction:column;gap:5px}.meeting-minutes .meeting-modal-head small{font-size:11px;color:#889d90}
.meeting-minutes-note{font-size:11px;line-height:1.6;color:#748c7b;margin:0 0 15px}.meeting-minutes-wait{display:flex;align-items:center;justify-content:center;gap:9px;padding:35px;color:#4b7c61;font-size:12px}
.meeting-minutes-fail{padding:25px 8px;text-align:center;color:#99594d;font-size:12px}.meeting-minutes-fail button{margin-left:12px;border:0;background:#e6f2e8;border-radius:5px;padding:6px 12px;color:#267451}
.meeting-minutes-group{margin-top:18px}.meeting-minutes-group h3{font-size:12px;color:#2d5e45;margin:0 0 9px}.meeting-minutes-empty{font-size:11px;color:#a0afa4}
.meeting-minutes-item{width:100%;display:flex;flex-direction:column;align-items:flex-start;gap:7px;text-align:left;background:#f7faf8;border:1px solid #e5eee8;border-radius:7px;padding:11px 13px;margin-bottom:7px;color:#304638;font-size:12px;line-height:1.6}.meeting-minutes-item:hover{background:#eaf4ed;border-color:#bad7c5}.meeting-minutes-item small{font-size:10px;color:#82978a}
@media(max-width:560px){.meeting-chat-head{padding:0 9px}.meeting-head-actions{gap:4px}.meeting-members-button{padding:0 6px;font-size:10px}.meeting-minutes{padding:17px}}
</style>
