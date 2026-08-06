<script setup lang="ts">
/**
 * 智能体 CLI 配置 弹窗(动态表单)
 *
 * 复用 PasswordDialog / GitHubDialog 的视觉语言
 * (mask + card + header/body/footer,dialog-fade transition,Teleport to body)。
 *
 * 根据 meta.credential_fields 动态渲染输入框:
 * - secret 类型:password 输入框 + 眼睛切换显隐,font-mono 便于核对 token
 * - text 类型:普通明文输入框
 *
 * secret 字段已配置时占位提示「已设置,留空保留」;未配置时用字段定义的 placeholder。
 * 草稿在 open=true 时重置;保存/清除/测试由父组件调 API 持久化。
 *
 * 测试连接:已配置凭据时显示「测试连接」按钮,父组件调
 * POST /agents/configs/{type}/test(SSE 流式)启动临时沙箱验证 PAT 有效性,
 * 流式推送阶段进度/模型思考/模型回答,结果通过 testResult prop 回传展示。
 */
import { computed, nextTick, reactive, ref, watch } from 'vue'
import type {
  AgentConfigDetailOut,
  AgentTestResult,
  AgentTypeMeta,
  CredentialField,
  CredentialValue,
} from '@/types/agent_configs'

const props = defineProps<{
  /** 是否显示 */
  open: boolean
  /** 类型元数据(含 credential_fields 定义),null 时弹窗不渲染表单 */
  meta: AgentTypeMeta | null
  /** 当前配置状态(用于判断各字段是否已设置),null 表示加载中 */
  detail: AgentConfigDetailOut | null
  /** 提交中状态(禁用按钮 + spinner) */
  saving: boolean
  /** 错误信息(父组件 API 失败时传入) */
  error?: string
  /** 测试连接中(禁用测试按钮 + spinner) */
  testing?: boolean
  /** 测试结果(父组件调 test API 后传入),null 表示未测试 */
  testResult?: AgentTestResult | null
  /** 当前测试阶段消息(流式,testing 时实时更新) */
  testStage?: string
  /** 模型思考过程增量累积(流式,testing 时实时更新) */
  testThinking?: string
  /** 模型回答增量累积(流式,testing 时实时更新) */
  testContent?: string
}>()

const emit = defineEmits<{
  (e: 'save', credentials: CredentialValue[], is_active: boolean): void
  (e: 'clear'): void
  (e: 'cancel'): void
  /** 测试连接(父组件调 API,结果通过 testResult prop 回传) */
  (e: 'test'): void
}>()

/** 字段值草稿:key 为字段 key,value 为输入框当前值 */
const draft = reactive<{
  values: Record<string, string>
  /** 各字段的显隐状态(secret 类型用),key 为字段 key */
  show: Record<string, boolean>
  is_active: boolean
}>({
  values: {},
  show: {},
  is_active: true,
})

/** open 变 true 时重置草稿;detail 异步加载完后再初始化一次(回显非 secret 字段)
 *
 * 监听 [open, detail]:
 * - open=true 时初始化(detail 此时可能为 null,用空值/default)
 * - detail 从 null 变成有值时(异步加载完成)重新初始化,回显已配置的非 secret 字段
 * 加载期间 saving=true(含 agentDialogLoading)使输入框 disabled,
 * 用户无法在 detail 到达前输入,不会被覆盖。
 */
watch(
  () => [props.open, props.detail] as const,
  ([isOpen]) => {
    if (!isOpen) return
    const fields = props.meta?.credential_fields ?? []
    const values: Record<string, string> = {}
    const show: Record<string, boolean> = {}
    // 非 secret 字段已配置值(后端回传),secret 字段不在此 dict
    const savedValues = props.detail?.credential_values ?? {}
    for (const f of fields) {
      if (f.type === 'secret') {
        // secret 字段:不回显原文,留空(已配置时 placeholder 提示"已设置,留空保留")
        values[f.key] = ''
      } else if (f.key in savedValues) {
        // 非 secret 字段:已配置则回显
        values[f.key] = savedValues[f.key]
      } else if (f.type === 'select' && f.default) {
        // select 字段未配置时用默认值
        values[f.key] = f.default
      } else {
        values[f.key] = ''
      }
      show[f.key] = false
    }
    draft.values = values
    draft.show = show
    // 启用状态:已配置时沿用当前值,未配置时默认启用
    draft.is_active = props.detail?.is_active ?? true
  },
  { immediate: true },
)

/** 是否已配置(决定 footer 按钮文案与是否显示「清除配置」) */
const hasCredentials = computed(
  () => !!props.detail && props.detail.has_credentials,
)

/** 指定字段是否已设置(用于 secret 字段的占位提示) */
function isFieldSet(field: CredentialField): boolean {
  return props.detail?.credential_status?.[field.key] === true
}

/** 指定字段的占位提示 */
function fieldPlaceholder(field: CredentialField): string {
  if (field.type === 'secret' && isFieldSet(field)) {
    return '已设置,留空保留'
  }
  return field.placeholder
}

/** 字段校验错误信息(空串=无错误) */
function fieldError(field: CredentialField): string {
  if (!field.required) return ''
  const val = (draft.values[field.key] ?? '').trim()
  // secret 字段已配置时允许留空(保留原值)
  if (field.type === 'secret' && isFieldSet(field)) return ''
  if (!val) return `请输入${field.label}`
  return ''
}

/** 是否所有必填字段都满足提交条件 */
const canSubmit = computed(() => {
  if (props.saving) return false
  const fields = props.meta?.credential_fields ?? []
  return fields.every((f) => !fieldError(f))
})

function handleSubmit(): void {
  if (!canSubmit.value || !props.meta) return
  const credentials: CredentialValue[] = props.meta.credential_fields.map((f) => ({
    key: f.key,
    value: draft.values[f.key] ?? '',
  }))
  emit('save', credentials, draft.is_active)
}

function handleClear(): void {
  if (props.saving) return
  emit('clear')
}

function handleTest(): void {
  // 测试中或保存中不允许重复触发
  if (props.testing || props.saving) return
  emit('test')
}

function handleCancel(): void {
  // 提交中/测试中不允许取消
  if (props.saving || props.testing) return
  emit('cancel')
}

/** 流式日志容器 ref(测试中自动滚动到底部) */
const streamLogRef = ref<HTMLElement | null>(null)

/** thinking/content 有增量时自动滚到底部 */
watch(
  () => [props.testThinking, props.testContent, props.testStage],
  () => {
    if (!props.testing) return
    nextTick(() => {
      const el = streamLogRef.value
      if (el) el.scrollTop = el.scrollHeight
    })
  },
)
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open && meta" class="dialog-mask" @click.self="handleCancel">
        <div class="dialog-card" role="dialog" aria-modal="true">
          <header class="dialog-header">
            <h3>{{ meta.display_name }}</h3>
            <button
              class="dialog-close"
              :disabled="saving"
              aria-label="关闭"
              @click="handleCancel"
            >×</button>
          </header>

          <div class="dialog-body">
            <p class="dialog-tip">
              {{ meta.description }}
              <a
                v-if="meta.help_url"
                :href="meta.help_url"
                target="_blank"
                rel="noopener noreferrer"
                class="tip-link"
              >获取帮助 →</a>
            </p>

            <!-- 当前状态 -->
            <div :class="['status-row', hasCredentials ? 'status-ok' : 'status-warn']">
              <span class="status-dot" />
              <span class="status-text">
                {{ hasCredentials ? '当前已配置凭据' : '尚未配置凭据' }}
              </span>
            </div>

            <!-- 动态凭据字段 -->
            <div
              v-for="field in meta.credential_fields"
              :key="field.key"
              class="field"
            >
              <label :for="`agent-field-${field.key}`">
                {{ field.label }}
                <span v-if="field.required" class="required-mark">*</span>
              </label>

              <!-- secret 类型:password 输入 + 眼睛切换显隐 -->
              <div v-if="field.type === 'secret'" class="input-wrapper">
                <input
                  :id="`agent-field-${field.key}`"
                  v-model="draft.values[field.key]"
                  :type="draft.show[field.key] ? 'text' : 'password'"
                  autocomplete="off"
                  :placeholder="fieldPlaceholder(field)"
                  :class="{ invalid: fieldError(field) }"
                  :disabled="saving"
                />
                <button
                  type="button"
                  class="toggle-pwd"
                  :aria-label="draft.show[field.key] ? '隐藏' : '显示'"
                  @click="draft.show[field.key] = !draft.show[field.key]"
                >
                  <svg v-if="!draft.show[field.key]" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                    <circle cx="12" cy="12" r="3" />
                  </svg>
                  <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                    <line x1="1" y1="1" x2="23" y2="23" />
                  </svg>
                </button>
              </div>

              <!-- text 类型:普通明文输入 -->
              <input
                v-else-if="field.type === 'text'"
                :id="`agent-field-${field.key}`"
                v-model="draft.values[field.key]"
                type="text"
                autocomplete="off"
                :placeholder="fieldPlaceholder(field)"
                :class="{ invalid: fieldError(field) }"
                :disabled="saving"
              />

              <!-- select 类型:下拉选择(如 provider_type) -->
              <select
                v-else-if="field.type === 'select'"
                :id="`agent-field-${field.key}`"
                v-model="draft.values[field.key]"
                :class="{ invalid: fieldError(field) }"
                :disabled="saving"
              >
                <option
                  v-for="opt in field.options"
                  :key="opt.value"
                  :value="opt.value"
                >
                  {{ opt.label }}
                </option>
              </select>

              <span v-if="field.help_text" class="field-help">{{ field.help_text }}</span>
              <a
                v-if="field.help_url"
                :href="field.help_url"
                target="_blank"
                rel="noopener noreferrer"
                class="field-help-link"
              >如何获取? →</a>
              <span v-if="fieldError(field)" class="field-error">{{ fieldError(field) }}</span>
            </div>

            <!-- 启用开关 -->
            <label class="active-toggle">
              <input
                v-model="draft.is_active"
                type="checkbox"
                :disabled="saving || testing"
              />
              <span>启用此执行器(任务提交页可选)</span>
            </label>

            <!-- 测试连接结果区(已配置凭据时显示) -->
            <div v-if="hasCredentials" class="test-section">
              <button
                class="btn btn-test"
                :disabled="saving || testing"
                @click="handleTest"
              >
                <span v-if="testing" class="btn-spinner test-spinner" />
                {{ testing ? '测试中...' : '测试连接' }}
              </button>

              <!-- 流式进度(测试中显示):阶段 + 思考 + 回答 -->
              <div
                v-if="testing && (testStage || testThinking || testContent)"
                ref="streamLogRef"
                class="test-stream"
              >
                <!-- 当前阶段 -->
                <div v-if="testStage" class="stream-stage">
                  <span class="stage-dot" />
                  <span class="stage-text">{{ testStage }}</span>
                </div>
                <!-- 模型思考 -->
                <div v-if="testThinking" class="stream-block stream-thinking">
                  <div class="stream-label">思考</div>
                  <div class="stream-body">{{ testThinking }}</div>
                </div>
                <!-- 模型回答 -->
                <div v-if="testContent" class="stream-block stream-content">
                  <div class="stream-label">回答</div>
                  <div class="stream-body">{{ testContent }}</div>
                </div>
              </div>

              <!-- 测试结果 -->
              <div
                v-if="testResult"
                :class="['test-result', testResult.ok ? 'test-ok' : 'test-fail']"
              >
                <span class="test-icon">{{ testResult.ok ? '✓' : '✗' }}</span>
                <span class="test-message">{{ testResult.message }}</span>
              </div>
            </div>
          </div>

          <footer class="dialog-footer">
            <span v-if="error" class="validation-error">{{ error }}</span>
            <span v-else></span>
            <div class="footer-actions">
              <!-- 已配置时显示「清除配置」按钮 -->
              <button
                v-if="hasCredentials"
                class="btn btn-danger"
                :disabled="saving || testing"
                @click="handleClear"
              >
                <span v-if="saving" class="btn-spinner danger" />
                清除配置
              </button>
              <button
                class="btn btn-secondary"
                :disabled="saving || testing"
                @click="handleCancel"
              >{{ hasCredentials ? '关闭' : '取消' }}</button>
              <button
                class="btn btn-primary"
                :disabled="!canSubmit || testing"
                @click="handleSubmit"
              >
                <span v-if="saving" class="btn-spinner" />
                {{ saving ? '处理中...' : (hasCredentials ? '更新' : '保存') }}
              </button>
            </div>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.dialog-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: var(--space-4);
}

.dialog-card {
  background: var(--color-surface);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-xl);
  width: 100%;
  max-width: 460px;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-border);
}

.dialog-header h3 {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  margin: 0;
  color: var(--color-text);
}

.dialog-close {
  background: none;
  border: none;
  font-size: 24px;
  line-height: 1;
  color: var(--color-text-muted);
  cursor: pointer;
  padding: 4px 8px;
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
}

.dialog-close:hover:not(:disabled) {
  background: var(--color-surface-alt);
  color: var(--color-text);
}

.dialog-close:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.dialog-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-5);
}

.dialog-tip {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  padding: var(--space-3);
  border-radius: var(--radius-md);
  border-left: 3px solid var(--color-primary);
  margin: 0 0 var(--space-4);
  line-height: var(--lh-relaxed);
}

.tip-link {
  color: var(--color-primary);
  text-decoration: none;
  margin-left: var(--space-1);
}

.tip-link:hover {
  text-decoration: underline;
}

/* ---- 状态行 ---- */
.status-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  margin-bottom: var(--space-4);
  font-size: var(--fs-sm);
}

.status-ok {
  background: var(--color-success-light);
  color: var(--color-success);
}

.status-warn {
  background: #fef3c7;
  color: #92400e;
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
  flex-shrink: 0;
}

.status-text {
  font-weight: var(--fw-medium);
}

/* ---- 表单字段 ---- */
.field {
  margin-bottom: var(--space-4);
}

.field:last-of-type {
  margin-bottom: var(--space-4);
}

.field label {
  display: block;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  margin-bottom: var(--space-2);
  color: var(--color-text);
}

.required-mark {
  color: var(--color-danger);
  margin-left: 2px;
}

/* secret 字段用 monospace 便于核对 token */
.field input,
.field select {
  width: 100%;
  height: 42px;
  padding: 0 var(--space-3);
  font-size: var(--fs-base);
  font-family: var(--font-mono);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.field input::placeholder {
  color: var(--color-text-muted);
  font-family: var(--font-base);
}

.field input:focus,
.field select:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.field input:disabled,
.field select:disabled {
  background: var(--color-surface-alt);
  cursor: not-allowed;
}

.field input.invalid,
.field select.invalid {
  border-color: var(--color-danger);
}

.input-wrapper {
  position: relative;
}

.input-wrapper input {
  padding-right: 44px;
}

.toggle-pwd {
  position: absolute;
  right: var(--space-2);
  top: 50%;
  transform: translateY(-50%);
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  color: var(--color-text-muted);
  border: none;
  background: transparent;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.toggle-pwd:hover {
  color: var(--color-text);
}

.field-help {
  display: block;
  margin-top: var(--space-1);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.field-help-link {
  display: inline-block;
  margin-top: var(--space-1);
  font-size: var(--fs-xs);
  color: var(--color-primary);
  text-decoration: none;
}

.field-help-link:hover {
  text-decoration: underline;
}

.field-error {
  display: block;
  margin-top: var(--space-1);
  font-size: var(--fs-xs);
  color: var(--color-danger);
}

/* ---- 启用开关 ---- */
.active-toggle {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-sm);
  color: var(--color-text);
  cursor: pointer;
  padding: var(--space-2) 0;
}

.active-toggle input {
  cursor: pointer;
  width: auto;
  height: auto;
}

/* ---- footer ---- */
.dialog-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-5);
  border-top: 1px solid var(--color-border);
}

/* ---- 测试连接区 ---- */
.test-section {
  margin-top: var(--space-4);
  padding-top: var(--space-4);
  border-top: 1px dashed var(--color-border);
}

.btn-test {
  background: var(--color-surface);
  color: var(--color-text-secondary);
  border-color: var(--color-border-strong);
  width: 100%;
}

.btn-test:hover:not(:disabled) {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.test-spinner {
  border-color: rgba(100, 116, 139, 0.3);
  border-top-color: var(--color-text-secondary);
}

.test-result {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  margin-top: var(--space-3);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
}

.test-ok {
  background: var(--color-success-light);
  color: var(--color-success);
}

.test-fail {
  background: #fef2f2;
  color: var(--color-danger);
}

.test-icon {
  font-weight: var(--fw-bold);
  flex-shrink: 0;
}

.test-message {
  word-break: break-word;
}

/* ---- 流式进度区 ---- */
.test-stream {
  margin-top: var(--space-3);
  padding: var(--space-3);
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  max-height: 200px;
  overflow-y: auto;
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
}

.stream-stage {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--color-text-secondary);
  padding-bottom: var(--space-2);
  margin-bottom: var(--space-2);
  border-bottom: 1px dashed var(--color-border);
}

.stream-stage .stage-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-primary);
  flex-shrink: 0;
  animation: stage-pulse 1.2s ease-in-out infinite;
}

@keyframes stage-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}

.stream-stage .stage-text {
  font-weight: var(--fw-medium);
}

.stream-block {
  margin-top: var(--space-2);
}

.stream-block:first-of-type {
  margin-top: 0;
}

.stream-label {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: var(--space-1);
}

.stream-thinking .stream-label {
  color: #7c3aed;
}

.stream-content .stream-label {
  color: var(--color-primary);
}

.stream-body {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--color-text);
  font-family: var(--font-base);
}

.stream-thinking .stream-body {
  color: var(--color-text-secondary);
  font-style: italic;
}

.validation-error {
  font-size: var(--fs-sm);
  color: var(--color-danger);
  flex: 1;
}

.footer-actions {
  display: flex;
  gap: var(--space-2);
}

.btn {
  height: 38px;
  padding: 0 var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  border: 1px solid transparent;
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-primary {
  background: var(--color-primary);
  color: white;
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
  border-color: var(--color-border-strong);
  color: var(--color-text);
}

.btn-danger {
  background: transparent;
  color: var(--color-danger);
  border-color: var(--color-danger);
}

.btn-danger:hover:not(:disabled) {
  background: var(--color-danger);
  color: white;
}

.btn-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: white;
  border-radius: 50%;
  animation: btn-spin 0.8s linear infinite;
}

.btn-spinner.danger {
  border-color: rgba(220, 38, 38, 0.3);
  border-top-color: var(--color-danger);
}

@keyframes btn-spin {
  to { transform: rotate(360deg); }
}

/* 弹窗淡入淡出 */
.dialog-fade-enter-active,
.dialog-fade-leave-active {
  transition: opacity 0.2s ease;
}

.dialog-fade-enter-from,
.dialog-fade-leave-to {
  opacity: 0;
}
</style>
