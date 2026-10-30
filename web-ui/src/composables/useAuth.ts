import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { wsService } from '@/services/websocket'

// Global State
const username = ref<string | null>(localStorage.getItem('auth_username'))
const isLoggedIn = ref(localStorage.getItem('auth_logged_in') === 'true')
const authenticationRequired = ref(true)
let sessionCheck: Promise<void> | null = null

export function useAuth() {
  const router = useRouter()

  const isAuthenticated = computed(() => isLoggedIn.value)

  function setAuthenticated(user: string) {
    username.value = user
    isLoggedIn.value = true

    localStorage.setItem('auth_username', user)
    localStorage.setItem('auth_logged_in', 'true')

    // 启动 WebSocket 连接
    wsService.start()
  }

  function logout() {
    void fetch('/auth/logout', { method: 'POST' }).catch(() => {})
    username.value = null
    isLoggedIn.value = false
    localStorage.removeItem('auth_username')
    localStorage.removeItem('auth_logged_in')

    // 停止 WebSocket 连接
    wsService.stop()

    // Redirect to login if using router
    if (router) {
      router.push('/login')
    } else {
      window.location.href = '/login'
    }
  }

  async function checkSession(): Promise<void> {
    if (!sessionCheck) sessionCheck = (async () => {
      try {
        const response = await fetch('/auth/session')
        if (response.ok) { const data = await response.json(); authenticationRequired.value = data.authentication_required !== false; setAuthenticated(data.username); return }
      } catch { /* API requests will still fail closed when offline. */ }
      username.value = null; isLoggedIn.value = false
      localStorage.removeItem('auth_username'); localStorage.removeItem('auth_logged_in')
      wsService.stop()
    })()
    await sessionCheck
  }

  async function login(user: string, pass: string): Promise<boolean> {
    try {
      const response = await fetch('/auth/status', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ username: user, password: pass }),
      })

      if (response.ok) {
        setAuthenticated(user)
        return true
      } else {
        if (response.status === 503 || response.status === 429) {
          const detail = await response.json()
          throw new Error(detail.detail || '登录暂不可用')
        }
        return false
      }
    } catch (e) {
      throw e
    }
  }

  return {
    username,
    isAuthenticated,
    authenticationRequired,
    login,
    checkSession,
    logout
  }
}
