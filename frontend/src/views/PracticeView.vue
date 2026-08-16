<script setup lang="ts">
/**
 * 自适应练习页
 *
 * 两种界面(组件内切换,不走路由):
 * - 首页:练习统计(能力值/到期复习/正确率/题库规模)+ 薄弱点列表 +
 *   开始练习入口 + 题库管理(列表/归档)
 * - 会话:逐题作答(单选/判断),提交后即时判分 + 解析 + 知识点掌握度反馈;
 *   结束时显示本局统计
 *
 * 组卷由后端 selector 完成(到期复习 > 薄弱点 > 难度匹配 > 新题),答案不下发。
 * 题目来源:审计任务详情页「生成练习题」产出并确认入库。
 */
import { computed, onMounted, ref } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import {
  archiveQuestion,
  getPracticeStats,
  listQuestions,
  startSession,
  submitAnswer,
} from '@/api/practice'
import { extractErrorMessage } from '@/utils/error'
import type {
  PracticeStats,
  QuestionListItem,
  SessionQuestion,
  SubmitAnswerResponse,
} from '@/types/practice'

// ============================================================
// 历史任务侧栏(与首页/其他视图一致的折叠模式)
// ============================================================
/** 历史任务侧栏是否折叠(默认折叠) */
const workspaceCollapsed = ref(true)

function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}

// ============================================================
// Toast(与其他视图一致的本地实现)
// ============================================================
const toast = ref<{ msg: string; type: 'success' | 'error' } | null>(null)
function showToast(msg: string, type: 'success' | 'error'): void {
  toast.value = { msg, type }
  setTimeout(() => {
    toast.value = null
  }, 4000)
}

// ============================================================
// 界面状态机:home(首页) / session(答题中) / summary(本局统计)
// ============================================================
type ViewMode = 'home' | 'session' | 'summary'
const mode = ref<ViewMode>('home')

// ============================================================
// 首页:统计 + 薄弱点
// ============================================================
const stats = ref<PracticeStats | null>(null)
const statsLoading = ref(true)
const statsError = ref('')

async function loadStats(): Promise<void> {
  statsLoading.value = true
  statsError.value = ''
  try {
    stats.value = await getPracticeStats()
  } catch (err) {
    statsError.value = extractErrorMessage(err)
  } finally {
    statsLoading.value = false
  }
}

// ============================================================
// 开始练习(count 可调;topic_filter 为空表示全部知识点)
// ============================================================
const sessionCount = ref(8)
const starting = ref(false)
const startError = ref('')

/** 会话数据 */
const sessionId = ref('')
const sessionQuestions = ref<SessionQuestion[]>([])
const currentIndex = ref(0)
const currentQuestion = computed<SessionQuestion | null>(
  () => sessionQuestions.value[currentIndex.value] ?? null,
)

/** 本局作答结果(用于统计与回顾) */
const sessionResults = ref<{ question: SessionQuestion; correct: boolean }[]>([])

async function handleStartPractice(topicFilter?: string): Promise<void> {
  if (starting.value) return
  starting.value = true
  startError.value = ''
  try {
    const res = await startSession({
      count: sessionCount.value,
      topic_filter: topicFilter ?? null,
    })
    if (res.message) showToast(res.message, 'success')
    sessionId.value = res.session_id
    sessionQuestions.value = res.questions
    currentIndex.value = 0
    sessionResults.value = []
    chosenIdx.value = null
    feedback.value = null
    submitting.value = false
    mode.value = 'session'
  } catch (err) {
    startError.value = extractErrorMessage(err)
  } finally {
    starting.value = false
  }
}

// ============================================================
// 会话:作答
// ============================================================
const chosenIdx = ref<number | null>(null)
const submitting = ref(false)
const feedback = ref<SubmitAnswerResponse | null>(null)

const isLastQuestion = computed(
  () => currentIndex.value >= sessionQuestions.value.length - 1,
)

async function handleSubmit(): Promise<void> {
  const q = currentQuestion.value
  if (!q || chosenIdx.value === null || submitting.value || feedback.value) return
  submitting.value = true
  try {
    const res = await submitAnswer(sessionId.value, {
      question_id: q.id,
      chosen_idx: chosenIdx.value,
    })
    feedback.value = res
    sessionResults.value.push({ question: q, correct: res.is_correct })
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
  } finally {
    submitting.value = false
  }
}

function handleNext(): void {
  if (isLastQuestion.value) {
    mode.value = 'summary'
    // 后台刷新统计,返回首页时展示最新掌握度
    loadStats()
    return
  }
  currentIndex.value += 1
  chosenIdx.value = null
  feedback.value = null
}

/** 提前结束本局(未完成全部题目) */
function handleQuitSession(): void {
  if (sessionResults.value.length === 0) {
    backToHome()
    return
  }
  mode.value = 'summary'
  loadStats()
}

function backToHome(): void {
  mode.value = 'home'
  sessionId.value = ''
  sessionQuestions.value = []
  sessionResults.value = []
  feedback.value = null
  loadStats()
  loadQuestionBank()
}

/** 本局正确率(summary 页展示) */
const sessionAccuracy = computed(() => {
  const total = sessionResults.value.length
  if (!total) return null
  return sessionResults.value.filter((r) => r.correct).length / total
})

// ============================================================
// 题库管理
// ============================================================
type BankFilter = 'active' | 'draft' | 'archived'
const bankFilter = ref<BankFilter>('active')
const bankQuestions = ref<QuestionListItem[]>([])
const bankLoading = ref(false)
const bankError = ref('')

async function loadQuestionBank(): Promise<void> {
  bankLoading.value = true
  bankError.value = ''
  try {
    bankQuestions.value = await listQuestions({ status: bankFilter.value })
  } catch (err) {
    bankError.value = extractErrorMessage(err)
  } finally {
    bankLoading.value = false
  }
}

function switchBankFilter(next: BankFilter): void {
  if (bankFilter.value === next) return
  bankFilter.value = next
  loadQuestionBank()
}

async function handleArchive(q: QuestionListItem): Promise<void> {
  try {
    await archiveQuestion(q.id)
    showToast('已归档,该题不再参与组卷', 'success')
    loadQuestionBank()
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
  }
}

// ============================================================
// 展示辅助
// ============================================================
function formatPercent(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `${(v * 100).toFixed(digits)}%`
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleDateString('zh-CN')
  } catch {
    return iso
  }
}

/** 难度展示(保留 1 位小数,整数不带小数) */
function formatDifficulty(d: number): string {
  return Number.isInteger(d) ? String(d) : d.toFixed(1)
}

/** 到期复习徽标:weak point 的 due_at 已过 → 显示「待复习」 */
function isDue(iso: string | null | undefined): boolean {
  if (!iso) return false
  const d = new Date(iso)
  return !Number.isNaN(d.getTime()) && d.getTime() <= Date.now()
}

onMounted(() => {
  loadStats()
  loadQuestionBank()
})
</script>

<template>
  <div class="page">
    <AppHeader>
      <template #leading>
        <WorkspaceToggleButton
          :collapsed="workspaceCollapsed"
          expand-title="展开历史任务"
          collapse-title="折叠历史任务"
          @toggle="toggleWorkspace"
        />
      </template>
    </AppHeader>

    <div class="page-body">
      <WorkspaceSidebar v-if="!workspaceCollapsed" />

      <main class="main">
      <!-- ============ 答题会话 ============ -->
      <template v-if="mode === 'session' && currentQuestion">
        <div class="session-bar">
          <button class="btn-ghost" @click="handleQuitSession">退出练习</button>
          <div class="session-progress">
            <span class="session-progress-text">
              第 {{ currentIndex + 1 }} / {{ sessionQuestions.length }} 题
            </span>
            <div class="progress-track">
              <div
                class="progress-fill"
                :style="{ width: `${(currentIndex / sessionQuestions.length) * 100}%` }"
              />
            </div>
          </div>
        </div>

        <div class="question-card">
          <div class="question-tags">
            <span v-if="currentQuestion.knowledge_name" class="tag tag-kp">
              {{ currentQuestion.knowledge_name }}
            </span>
            <span class="tag">{{ currentQuestion.qtype === 'true_false' ? '判断题' : '单选题' }}</span>
            <span class="tag">难度 {{ formatDifficulty(currentQuestion.difficulty) }}</span>
          </div>

          <h2 class="question-stem">{{ currentQuestion.stem }}</h2>

          <pre v-if="currentQuestion.code_snippet" class="code-snippet"><code>{{ currentQuestion.code_snippet }}</code></pre>

          <div class="options">
            <button
              v-for="(opt, idx) in currentQuestion.options"
              :key="idx"
              type="button"
              :class="[
                'option',
                {
                  'option-selected': !feedback && chosenIdx === idx,
                  'option-correct': feedback && idx === feedback.correct_idx,
                  'option-wrong': feedback && chosenIdx === idx && !feedback.is_correct,
                  'option-dimmed': feedback && idx !== feedback.correct_idx && chosenIdx !== idx,
                },
              ]"
              :disabled="!!feedback"
              @click="chosenIdx = idx"
            >
              <span class="option-key">{{ String.fromCharCode(65 + idx) }}</span>
              <span class="option-text">{{ opt }}</span>
            </button>
          </div>

          <!-- 未作答:提交按钮;已作答:判分反馈 -->
          <div v-if="!feedback" class="question-actions">
            <button
              class="btn-primary"
              :disabled="chosenIdx === null || submitting"
              @click="handleSubmit"
            >
              {{ submitting ? '提交中...' : '提交答案' }}
            </button>
          </div>

          <div v-else :class="['feedback', feedback.is_correct ? 'feedback-correct' : 'feedback-wrong']">
            <div class="feedback-head">
              <span class="feedback-verdict">{{ feedback.is_correct ? '回答正确' : '回答错误' }}</span>
              <span v-if="feedback.state" class="feedback-kp">
                {{ feedback.state.knowledge_name }} · 正确率
                {{ formatPercent(feedback.state.accuracy) }}({{ feedback.state.correct_count }}/{{ feedback.state.attempts }})
                <template v-if="feedback.state.due_at"> · 下次复习 {{ formatDate(feedback.state.due_at) }}</template>
              </span>
            </div>
            <p v-if="feedback.explanation" class="feedback-explanation">{{ feedback.explanation }}</p>
            <div class="question-actions">
              <button class="btn-primary" @click="handleNext">
                {{ isLastQuestion ? '查看本局统计' : '下一题' }}
              </button>
            </div>
          </div>
        </div>
      </template>

      <!-- ============ 本局统计 ============ -->
      <template v-else-if="mode === 'summary'">
        <div class="summary-card">
          <h2>练习完成</h2>
          <div class="summary-grid">
            <div class="summary-item">
              <span class="summary-value">{{ sessionResults.length }}</span>
              <span class="summary-label">作答题数</span>
            </div>
            <div class="summary-item">
              <span class="summary-value">{{ sessionResults.filter((r) => r.correct).length }}</span>
              <span class="summary-label">答对</span>
            </div>
            <div class="summary-item">
              <span class="summary-value">{{ sessionResults.filter((r) => !r.correct).length }}</span>
              <span class="summary-label">答错</span>
            </div>
            <div class="summary-item">
              <span class="summary-value">{{ formatPercent(sessionAccuracy) }}</span>
              <span class="summary-label">正确率</span>
            </div>
          </div>

          <div v-if="sessionResults.length" class="summary-list">
            <div
              v-for="(r, idx) in sessionResults"
              :key="idx"
              :class="['summary-row', r.correct ? 'summary-row-correct' : 'summary-row-wrong']"
            >
              <span class="summary-row-mark">{{ r.correct ? '✓' : '✗' }}</span>
              <span class="summary-row-stem">{{ r.question.stem }}</span>
            </div>
          </div>

          <div class="summary-actions">
            <button class="btn-secondary" @click="backToHome">返回首页</button>
            <button class="btn-primary" :disabled="starting" @click="handleStartPractice()">
              再来一局
            </button>
          </div>
        </div>
      </template>

      <!-- ============ 首页 ============ -->
      <template v-else>
        <header class="page-head">
          <h1>自适应练习</h1>
          <p>题目来自审计任务的真实发现 · 到期复习优先,薄弱点强化,按能力匹配难度</p>
        </header>

        <!-- 加载中 / 失败 -->
        <div v-if="statsLoading" class="placeholder">
          <span class="status-spinner" /> 加载中...
        </div>
        <div v-else-if="statsError" class="placeholder error-text">
          加载失败: {{ statsError }}
          <button class="btn-link" @click="loadStats">重试</button>
        </div>

        <template v-else-if="stats">
          <!-- 统计卡片 -->
          <div class="stat-cards">
            <div class="stat-card">
              <span class="stat-value">{{ formatDifficulty(stats.ability) }}</span>
              <span class="stat-label">能力估计</span>
            </div>
            <div :class="['stat-card', { 'stat-card-alert': stats.due_count > 0 }]">
              <span class="stat-value">{{ stats.due_count }}</span>
              <span class="stat-label">到期待复习知识点</span>
            </div>
            <div class="stat-card">
              <span class="stat-value">{{ formatPercent(stats.accuracy) }}</span>
              <span class="stat-label">累计正确率({{ stats.total_attempts }} 题)</span>
            </div>
            <div class="stat-card">
              <span class="stat-value">{{ stats.active_question_count }}</span>
              <span class="stat-label">题库题数<template v-if="stats.draft_question_count">(另有 {{ stats.draft_question_count }} 待确认)</template></span>
            </div>
          </div>

          <!-- 开始练习 -->
          <section class="panel">
            <div class="start-row">
              <div class="start-left">
                <h2>开始练习</h2>
                <p v-if="stats.active_question_count === 0" class="start-hint">
                  题库为空 — 请到已完成审计任务的详情页,点「结果清单」旁的「生成练习题」把真实发现转成题目
                </p>
                <p v-else-if="stats.due_count > 0" class="start-hint">
                  有 {{ stats.due_count }} 个知识点到期待复习,本次组卷将优先安排复习题
                </p>
              </div>
              <div class="start-controls">
                <label class="count-label">
                  题数
                  <select v-model.number="sessionCount" class="count-select" :disabled="starting">
                    <option v-for="n in [4, 8, 12, 16]" :key="n" :value="n">{{ n }}</option>
                  </select>
                </label>
                <button
                  class="btn-primary"
                  :disabled="starting || stats.active_question_count === 0"
                  @click="handleStartPractice()"
                >
                  {{ starting ? '组卷中...' : '开始练习' }}
                </button>
              </div>
            </div>
            <p v-if="startError" class="action-error">{{ startError }}</p>
          </section>

          <!-- 薄弱点 -->
          <section class="panel">
            <h2>薄弱知识点</h2>
            <p v-if="stats.weak_points.length === 0" class="panel-empty">
              暂无作答记录 — 完成几轮练习后,这里会按错误率展示各知识点掌握情况
            </p>
            <div v-else class="weak-list">
              <div v-for="w in stats.weak_points" :key="w.knowledge_key" class="weak-item">
                <div class="weak-info">
                  <span class="weak-name">{{ w.knowledge_name }}</span>
                  <span class="weak-key">{{ w.knowledge_key }}</span>
                  <span v-if="isDue(w.due_at)" class="tag tag-due">待复习</span>
                </div>
                <div class="weak-bar-wrap" :title="`错误率 ${formatPercent(w.accuracy)}`">
                  <div class="weak-bar" :style="{ width: `${Math.min(w.accuracy * 100, 100)}%` }" />
                </div>
                <span class="weak-stat">
                  错 {{ formatPercent(w.accuracy) }} · {{ w.attempts }} 次作答
                </span>
                <button
                  class="btn-secondary btn-small"
                  :disabled="starting || stats.active_question_count === 0"
                  title="只练习该知识点的题目"
                  @click="handleStartPractice(w.knowledge_key)"
                >专项练习</button>
              </div>
            </div>
          </section>

          <!-- 题库管理 -->
          <section class="panel">
            <div class="bank-head">
              <h2>题库管理</h2>
              <div class="bank-filters" role="group" aria-label="题库状态筛选">
                <button
                  v-for="f in ([['active', '已入库'], ['draft', '待确认'], ['archived', '已归档']] as [BankFilter, string][])"
                  :key="f[0]"
                  :class="['mode-btn', { active: bankFilter === f[0] }]"
                  @click="switchBankFilter(f[0])"
                >{{ f[1] }}</button>
              </div>
            </div>
            <div v-if="bankLoading" class="placeholder"><span class="status-spinner" /> 加载中...</div>
            <p v-else-if="bankError" class="action-error">{{ bankError }}</p>
            <p v-else-if="bankQuestions.length === 0" class="panel-empty">该状态下暂无题目</p>
            <div v-else class="bank-list">
              <div v-for="q in bankQuestions" :key="q.id" class="bank-item">
                <div class="bank-item-main">
                  <span class="bank-stem">{{ q.stem }}</span>
                  <span class="bank-meta">
                    <span v-if="q.knowledge_name" class="tag tag-kp">{{ q.knowledge_name }}</span>
                    <span>{{ q.qtype === 'true_false' ? '判断' : '单选' }}</span>
                    <span>难度 {{ formatDifficulty(q.difficulty) }}</span>
                    <template v-if="q.attempts > 0">
                      <span>作答 {{ q.attempts }} 次 · 正确率 {{ formatPercent(q.accuracy) }}</span>
                    </template>
                    <span class="bank-date">{{ formatDate(q.created_at) }}</span>
                  </span>
                </div>
                <button
                  v-if="q.status !== 'archived'"
                  class="btn-secondary btn-small"
                  title="归档后不再参与组卷"
                  @click="handleArchive(q)"
                >归档</button>
              </div>
            </div>
          </section>
        </template>
      </template>
      </main>
    </div>

    <!-- ============ 浮动提示 ============ -->
    <Teleport to="body">
      <Transition name="toast-slide">
        <div
          v-if="toast"
          :class="['toast-popup', toast.type === 'error' ? 'toast-error' : 'toast-success']"
          role="status"
          aria-live="polite"
        >
          <span class="toast-msg">{{ toast.msg }}</span>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
  background: var(--color-bg);
}

.page-body {
  flex: 1;
  display: flex;
  align-items: stretch;
  min-height: 0;
  overflow: hidden;
}

.main {
  flex: 1;
  min-height: 0;
  min-width: 0;
  max-width: 860px;
  margin: 0 auto;
  overflow-y: auto;
  padding: var(--space-8) var(--space-6);
}

.page-head {
  margin-bottom: var(--space-6);
}

.page-head h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--fs-xl);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.page-head p {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.placeholder {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-6);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.placeholder.error-text {
  color: var(--color-danger);
}

.btn-link {
  background: none;
  border: none;
  padding: 4px 8px;
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  cursor: pointer;
}

.btn-link:hover {
  text-decoration: underline;
}

/* ============ 统计卡片 ============ */
.stat-cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--space-3);
  margin-bottom: var(--space-6);
}

.stat-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-4);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
}

.stat-card-alert {
  border-color: var(--color-primary);
}

.stat-value {
  font-size: var(--fs-xl);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.stat-card-alert .stat-value {
  color: var(--color-primary);
}

.stat-label {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

/* ============ 通用面板 ============ */
.panel {
  padding: var(--space-5);
  margin-bottom: var(--space-5);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
}

.panel h2 {
  margin: 0 0 var(--space-3);
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.panel-empty {
  margin: 0;
  padding: var(--space-3) 0;
  font-size: var(--fs-sm);
  color: var(--color-text-muted);
  line-height: var(--lh-relaxed);
}

.action-error {
  margin: var(--space-2) 0 0;
  font-size: var(--fs-xs);
  color: var(--color-danger);
}

/* ============ 开始练习 ============ */
.start-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.start-left h2 {
  margin-bottom: var(--space-1);
}

.start-hint {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  max-width: 460px;
  line-height: var(--lh-relaxed);
}

.start-controls {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.count-label {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.count-select {
  padding: var(--space-1) var(--space-2);
  font-size: var(--fs-sm);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

/* ============ 薄弱点列表 ============ */
.weak-list {
  display: flex;
  flex-direction: column;
}

.weak-item {
  display: grid;
  grid-template-columns: minmax(180px, 1.4fr) minmax(80px, 1fr) auto auto;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-3) 0;
  border-top: 1px solid var(--color-border);
}

.weak-item:first-child {
  border-top: none;
}

.weak-info {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
}

.weak-name {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.weak-key {
  font-size: var(--fs-xs);
  font-family: var(--font-mono);
  color: var(--color-text-muted);
  white-space: nowrap;
}

.weak-bar-wrap {
  height: 6px;
  background: var(--color-surface-alt);
  border-radius: var(--radius-full);
  overflow: hidden;
}

.weak-bar {
  height: 100%;
  background: var(--color-danger);
  border-radius: var(--radius-full);
}

.weak-stat {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  white-space: nowrap;
}

/* ============ 题库管理 ============ */
.bank-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.bank-filters {
  display: inline-flex;
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
  padding: 2px;
  gap: 2px;
}

.mode-btn {
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  background: transparent;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.mode-btn:hover:not(.active) {
  color: var(--color-text);
}

.mode-btn.active {
  background: var(--color-surface);
  color: var(--color-text);
  box-shadow: var(--shadow-sm);
}

.bank-list {
  display: flex;
  flex-direction: column;
}

.bank-item {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-3) 0;
  border-top: 1px solid var(--color-border);
}

.bank-item:first-child {
  border-top: none;
}

.bank-item-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.bank-stem {
  font-size: var(--fs-sm);
  color: var(--color-text);
  line-height: var(--lh-base);
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.bank-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.bank-date {
  margin-left: auto;
}

/* ============ 会话 ============ */
.session-bar {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  margin-bottom: var(--space-5);
}

.session-progress {
  flex: 1;
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.session-progress-text {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.progress-track {
  flex: 1;
  height: 6px;
  background: var(--color-surface-alt);
  border-radius: var(--radius-full);
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: var(--color-primary);
  border-radius: var(--radius-full);
  transition: width var(--transition-base);
}

.question-card {
  padding: var(--space-6);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
}

.question-tags {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
}

.tag {
  display: inline-flex;
  align-items: center;
  padding: 2px var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
  white-space: nowrap;
}

.tag-kp {
  color: var(--color-primary);
  background: var(--color-primary-light);
}

.tag-due {
  color: var(--color-warning, #b45309);
  background: color-mix(in srgb, var(--color-warning, #f59e0b) 15%, transparent);
}

.question-stem {
  margin: 0 0 var(--space-4);
  font-size: var(--fs-base);
  font-weight: var(--fw-medium);
  line-height: var(--lh-relaxed);
  color: var(--color-text);
  word-break: break-word;
}

.code-snippet {
  margin: 0 0 var(--space-4);
  padding: var(--space-3);
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
  overflow-x: auto;
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--color-text);
}

.options {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.option {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  width: 100%;
  padding: var(--space-3);
  font-size: var(--fs-sm);
  text-align: left;
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.option:hover:not(:disabled) {
  border-color: var(--color-primary);
}

.option-selected {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.option-correct {
  border-color: var(--color-success);
  background: var(--color-success-light);
}

.option-wrong {
  border-color: var(--color-danger);
  background: var(--color-danger-light);
}

.option-dimmed {
  opacity: 0.6;
}

.option:disabled {
  cursor: default;
}

.option-key {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
}

.option-text {
  flex: 1;
  line-height: var(--lh-relaxed);
  word-break: break-word;
}

.question-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--space-4);
}

.feedback {
  margin-top: var(--space-4);
  padding: var(--space-4);
  border-radius: var(--radius-md);
}

.feedback-correct {
  background: var(--color-success-light);
  border: 1px solid color-mix(in srgb, var(--color-success) 35%, transparent);
}

.feedback-wrong {
  background: var(--color-danger-light);
  border: 1px solid color-mix(in srgb, var(--color-danger) 35%, transparent);
}

.feedback-head {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.feedback-verdict {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
}

.feedback-correct .feedback-verdict {
  color: var(--color-success);
}

.feedback-wrong .feedback-verdict {
  color: var(--color-danger);
}

.feedback-kp {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
}

.feedback-explanation {
  margin: var(--space-2) 0 0;
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--color-text);
}

/* ============ 本局统计 ============ */
.summary-card {
  padding: var(--space-6);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
}

.summary-card h2 {
  margin: 0 0 var(--space-4);
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--space-3);
  margin-bottom: var(--space-5);
}

.summary-item {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-3);
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
}

.summary-value {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.summary-label {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.summary-list {
  display: flex;
  flex-direction: column;
  margin-bottom: var(--space-5);
}

.summary-row {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-2) 0;
  border-top: 1px solid var(--color-border);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.summary-row:first-child {
  border-top: none;
}

.summary-row-mark {
  flex-shrink: 0;
  font-weight: var(--fw-semibold);
}

.summary-row-correct .summary-row-mark {
  color: var(--color-success);
}

.summary-row-wrong .summary-row-mark {
  color: var(--color-danger);
}

.summary-row-stem {
  line-height: var(--lh-base);
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 1;
  -webkit-box-orient: vertical;
}

.summary-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-3);
}

/* ============ 按钮 ============ */
.btn-primary,
.btn-secondary,
.btn-ghost {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  height: 36px;
  padding: 0 var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  border: 1px solid transparent;
}

.btn-primary {
  background: var(--color-primary);
  color: var(--color-text-inverse);
}

.btn-primary:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.btn-secondary {
  background: var(--color-surface);
  color: var(--color-text-secondary);
  border-color: var(--color-border);
}

.btn-secondary:hover:not(:disabled) {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.btn-ghost {
  background: transparent;
  color: var(--color-text-secondary);
}

.btn-ghost:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.btn-small {
  height: 28px;
  padding: 0 var(--space-3);
  font-size: var(--fs-xs);
}

.btn-primary:disabled,
.btn-secondary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* spinner */
.status-spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: status-spin 0.8s linear infinite;
  flex-shrink: 0;
}

@keyframes status-spin {
  to { transform: rotate(360deg); }
}

/* ============ Toast ============ */
.toast-popup {
  position: fixed;
  top: var(--space-5);
  left: 50%;
  transform: translateX(-50%);
  z-index: 2000;
  max-width: 420px;
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  box-shadow: var(--shadow-lg);
  border: 1px solid transparent;
}

.toast-success {
  background: var(--color-success-light);
  color: var(--color-success);
}

.toast-error {
  background: var(--color-danger-light);
  color: var(--color-danger);
}

.toast-msg {
  word-break: break-word;
  white-space: pre-wrap;
}

.toast-slide-enter-active,
.toast-slide-leave-active {
  transition: opacity var(--transition-base), transform var(--transition-base);
}

.toast-slide-enter-from,
.toast-slide-leave-to {
  opacity: 0;
  transform: translate(-50%, -12px);
}
</style>
