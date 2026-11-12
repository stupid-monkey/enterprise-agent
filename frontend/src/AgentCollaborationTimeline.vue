<script setup lang="ts">
import { computed } from 'vue'
import { Check } from 'lucide-vue-next'
import type { Artifact, CollaborationRun, CollaborationStep } from './auth'

const props = defineProps<{
  artifact: Artifact | null
  run?: CollaborationRun
  running: boolean
  available: boolean
}>()

const stages = [
  { key: 'supervisor', name: '规划 Agent', task: '确定各章节的整理重点' },
  { key: 'knowledge', name: '证据 Agent', task: '从已授权摘录中选逐字引句' },
  { key: 'document', name: '撰写 Agent', task: '依据引句生成待核对建议' },
  { key: 'validator', name: '程序校验', task: '检查引句、章节和输出结构' }
] as const

const currentRun = computed(() => props.run?.draft_token === props.artifact?.draft_token ? props.run : undefined)
const failureReason = computed(() => {
  switch (currentRun.value?.error_code) {
    case 'MODEL_UNSUPPORTED_QUOTE': return '模型所选引句与资料原文不一致，已停止生成'
    case 'MODEL_OUTPUT_INVALID': return '模型输出的章节数量或格式不符合要求'
    case 'APITimeoutError': return '模型服务响应超时'
    case 'APIConnectionError': return '无法连接模型服务'
    case 'AuthenticationError': return '模型服务认证失败，请检查服务配置'
    case 'RateLimitError': return '模型服务请求过多，请稍后继续'
    case 'COLLABORATION_CALL_BUDGET_EXCEEDED': return '本次生成达到模型调用上限'
    case 'COLLABORATION_INPUT_TOO_LARGE': return '来源摘录过长，请减少资料或章节'
    case 'COLLABORATION_TIME_BUDGET_EXCEEDED': return '本次生成超过时间上限'
    default: return currentRun.value?.error_code ? '模型服务或校验过程异常' : ''
  }
})
const trace = computed(() => currentRun.value?.status === 'failed'
  ? currentRun.value.trace
  : props.artifact?.collaboration_trace ?? [])

function completedStep(key: string): CollaborationStep | undefined {
  return trace.value.find(step => step.agent === key)
}

function stageState(key: string, index: number): 'done' | 'failed' | 'running' | 'waiting' {
  if (completedStep(key)) return 'done'
  if (currentRun.value?.status === 'failed' && currentRun.value.stage === key) return 'failed'
  if (props.running && trace.value.length === index) return 'running'
  return 'waiting'
}

function stageResult(step: CollaborationStep): string {
  const output = step.output
  if (step.agent === 'supervisor' && Array.isArray(output.focus))
    return Array.isArray(output.source_ids)
      ? `已选 ${output.source_ids.length} 份来源、规划 ${output.focus.length} 节重点`
      : `已规划 ${output.focus.length} 节重点`
  if (step.agent === 'knowledge') {
    if (Array.isArray(output.evidence))
      return `已选 ${output.evidence.reduce<number>((sum, section) => sum + (Array.isArray(section) ? section.length : 0), 0)} 条引句`
    if (Array.isArray(output.quotes))
      return `已选 ${output.quotes.filter(quote => typeof quote === 'string' && quote.trim()).length} 条引句`
  }
  if (step.agent === 'document' && Array.isArray(output.drafts))
    return props.artifact?.kind === 'pptx'
      ? `已生成 ${output.drafts.filter(draft => typeof draft === 'string' && draft.trim()).length} 页初稿`
      : `已写 ${output.drafts.filter(draft => typeof draft === 'string' && draft.trim()).length} 段建议`
  if (step.agent === 'validator' && typeof output.supported_quotes === 'number')
    return `已校验 ${output.supported_quotes} 条引句`
  return '已完成交接'
}

const runStatus = computed(() => {
  if (!props.artifact) return props.available
    ? '先选择资料并生成草稿'
    : '未配置协作模型'
  if (props.artifact.source_status !== 'current') return '来源已变化，需更新草稿'
  if (props.running) return '协作运行中，完成后显示交接结果'
  if (currentRun.value?.status === 'failed') {
    const failed = stages.find(stage => stage.key === currentRun.value?.stage)
    return `${failed?.name ?? '保存草稿'}失败 · ${failureReason.value} · 已尝试 ${currentRun.value.attempts}/${currentRun.value.max_attempts} 次`
  }
  if (trace.value.length === stages.length)
    return props.artifact.kind === 'pptx' ? 'PPT 初稿已生成，仍需人工核对' : '协作已完成，建议仍需人工核对'
  if (!props.available) return '未配置协作模型'
  return props.artifact.kind === 'pptx' ? '待运行 · 点击生成 AI 初稿' : '待运行 · 点击 AI 协作整理'
})
</script>

<template>
  <section class="agent-timeline" aria-label="AI 协作流程">
    <div class="agent-timeline-heading">
      <strong>AI 协作流程</strong>
      <span>固定顺序 · 3 个模型角色 + 程序校验</span>
    </div>
    <p class="agent-timeline-status" :class="{ failed: currentRun?.status === 'failed', complete: currentRun?.status !== 'failed' && trace.length === stages.length }" role="status">{{ runStatus }}</p>
    <ol class="agent-stages">
      <li v-for="(stage, index) in stages" :key="stage.key" class="agent-stage" :class="`is-${stageState(stage.key, index)}`">
        <span class="agent-stage-number"><Check v-if="stageState(stage.key, index) === 'done'" :size="12" />{{ stageState(stage.key, index) === 'done' ? '' : index + 1 }}</span>
        <div class="agent-stage-content">
          <div class="agent-stage-title"><strong>{{ stage.name }}</strong><span>{{ stageState(stage.key, index) === 'done' ? '已完成' : stageState(stage.key, index) === 'failed' ? '失败' : stageState(stage.key, index) === 'running' ? '执行中' : '待执行' }}</span></div>
          <p>{{ stage.key === 'document' && artifact?.kind === 'pptx' ? '根据已核验引句撰写逐页初稿正文' : stage.task }}</p>
          <small v-if="completedStep(stage.key)">{{ stageResult(completedStep(stage.key)!) }} · {{ completedStep(stage.key)!.input_sections }} 节 · {{ completedStep(stage.key)!.elapsed_ms }} ms</small>
        </div>
      </li>
    </ol>
    <p v-if="currentRun?.status === 'failed' && currentRun.can_retry" class="agent-timeline-retry">再次点击“{{ artifact?.kind === 'pptx' ? '继续生成初稿' : '继续 AI 协作' }}”，将从未完成的节点恢复。</p>
  </section>
</template>
