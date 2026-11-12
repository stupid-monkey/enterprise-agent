<script setup lang="ts">
import { computed } from 'vue'
import { ChevronLeft, ChevronRight } from 'lucide-vue-next'

const props = defineProps<{ page: number; total: number; pageSize: number }>()
const emit = defineEmits<{ change: [page: number] }>()
const pageCount = computed(() => Math.max(1, Math.ceil(props.total / props.pageSize)))
const currentPage = computed(() => Math.min(Math.max(1, props.page), pageCount.value))
const pages = computed(() => {
  const start = Math.max(1, Math.min(currentPage.value - 2, pageCount.value - 4))
  return Array.from({ length: Math.min(5, pageCount.value - start + 1) }, (_, index) => start + index)
})
function change(page: number) {
  if (page >= 1 && page <= pageCount.value && page !== currentPage.value) emit('change', page)
}
</script>

<template>
  <nav v-if="total > 0" class="list-pager" aria-label="分页导航">
    <span class="list-pager-total">共 {{ total }} 条 · 第 {{ currentPage }}/{{ pageCount }} 页</span>
    <div class="list-pager-buttons">
      <button type="button" :disabled="currentPage === 1" aria-label="上一页" @click="change(currentPage - 1)"><ChevronLeft :size="15" /></button>
      <button v-for="number in pages" :key="number" type="button" :class="{ active: number === currentPage }" :aria-current="number === currentPage ? 'page' : undefined" :aria-label="`第 ${number} 页`" @click="change(number)">{{ number }}</button>
      <button type="button" :disabled="currentPage === pageCount" aria-label="下一页" @click="change(currentPage + 1)"><ChevronRight :size="15" /></button>
    </div>
  </nav>
</template>

<style scoped>
.list-pager{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:12px 15px;color:#84998a;font-size:11px}
.list-pager-buttons{display:flex;align-items:center;gap:5px}.list-pager-buttons button{min-width:29px;height:29px;display:grid;place-items:center;border:1px solid #dce8df;border-radius:5px;background:#fff;color:#587361;font-size:11px;cursor:pointer}
.list-pager-buttons button:hover:not(:disabled){border-color:#88bba0;background:#f1f8f3}.list-pager-buttons button.active{background:#087e6d;border-color:#087e6d;color:white;font-weight:700}.list-pager-buttons button:disabled{opacity:.42;cursor:not-allowed}
</style>
