import { ref, onMounted, onBeforeUnmount } from 'vue'
import type {
  Task,
  TaskCreateResponse,
  TaskGenerateRequest,
  TaskUpdate,
} from '@/types/task.d.ts'
import { isRevisitRunning } from '@/lib/taskRevisit'
import * as taskApi from '@/api/tasks'
import { useWebSocket } from '@/composables/useWebSocket'

export function useTasks() {
  const tasks = ref<Task[]>([])
  const isLoading = ref(false)
  const error = ref<Error | null>(null)
  const pendingRevisitIds=ref<Set<number>>(new Set())
  let revisitPoll:ReturnType<typeof setInterval>|null=null
  const stoppingTaskIds = ref<Set<number>>(new Set())
  const { on } = useWebSocket()

  async function fetchTasks(options?: { silent?: boolean }) {
    if (!options?.silent) {
      isLoading.value = true
    }
    error.value = null
    try {
      tasks.value = await taskApi.getAllTasks()
    } catch (e) {
      if (e instanceof Error) {
        error.value = e
      }
      console.error(e)
    } finally {
      if (!options?.silent) {
        isLoading.value = false
      }
    }
  }

  // Real-time updates
  on('tasks_updated', () => {
    fetchTasks({ silent: true })
  })

  on('task_status_changed', (data: { id: number; is_running: boolean }) => {
    const task = tasks.value.find((t) => t.id === data.id)
    if (task) {
      task.is_running = data.is_running
    }
    fetchTasks({ silent: true })
  })

  async function createTask(data: TaskGenerateRequest): Promise<TaskCreateResponse> {
    isLoading.value = true
    error.value = null
    try {
      return await taskApi.createTaskWithAI(data)
    } catch (e) {
      if (e instanceof Error) {
        error.value = e
      }
      console.error(e)
      throw e
    } finally {
      isLoading.value = false
    }
  }

  async function updateTask(taskId: number, data: TaskUpdate) {
    error.value = null
    try {
      const updatedTask = await taskApi.updateTask(taskId, data)
      const index = tasks.value.findIndex((task) => task.id === updatedTask.id)
      if (index >= 0) {
        tasks.value[index] = { ...tasks.value[index], ...updatedTask }
      } else {
        tasks.value.push(updatedTask)
      }
    } catch (e) {
      if (e instanceof Error) {
        error.value = e
      }
      console.error(e)
      throw e
    }
  }

  async function removeTask(taskId: number) {
    try {
      await taskApi.deleteTask(taskId)
      // Refresh the list after deleting
      await fetchTasks()
    } catch (e) {
      console.error(e)
      // Optionally, set the error ref to display it in the UI
      if (e instanceof Error) {
        error.value = e
      }
      throw e
    }
  }

  async function startTask(taskId: number) {
    isLoading.value = true
    const task = tasks.value.find((t) => t.id === taskId)
    const previous = task?.is_running
    if (task) {
      task.is_running = true // 乐观更新：点击后立刻显示运行中
    }
    try {
      await taskApi.startTask(taskId)
      // The websocket will update the status, but we can also optimistically update
    } catch (e) {
      if (task && previous !== undefined) {
        task.is_running = previous
      }
      if (e instanceof Error) error.value = e
      throw e
    } finally {
      isLoading.value = false
    }
  }

  async function stopTask(taskId: number) {
    isLoading.value = true
    const next = new Set(stoppingTaskIds.value)
    next.add(taskId)
    stoppingTaskIds.value = next
    try {
      await taskApi.stopTask(taskId)
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    } finally {
      const cleaned = new Set(stoppingTaskIds.value)
      cleaned.delete(taskId)
      stoppingTaskIds.value = cleaned
      isLoading.value = false
    }
  }
  
  async function startRevisit(taskId:number) {
    if (pendingRevisitIds.value.has(taskId) || isRevisitRunning(tasks.value.find(task=>task.id===taskId)?.revisit_run)) return
    pendingRevisitIds.value=new Set([...pendingRevisitIds.value,taskId])
    try {
      const accepted=await taskApi.startTaskRevisit(taskId)
      const task=tasks.value.find(task=>task.id===taskId)
      if (task) task.revisit_run={...accepted,reviewed:0,sold:0,lease_until:new Date(Date.now()+120000).toISOString()}
      await fetchTasks({silent:true})
    } catch (error) {
      await fetchTasks({silent:true})  // A 409 also restores another worker's progress.
      throw error
    } finally {
      const next=new Set(pendingRevisitIds.value);next.delete(taskId);pendingRevisitIds.value=next
    }
  }
  let polling=false
  onMounted(()=>{
    revisitPoll=setInterval(async()=>{
      if (polling || !tasks.value.some(task=>isRevisitRunning(task.revisit_run))) return
      polling=true
      try { await fetchTasks({silent:true}) } finally { polling=false }
    },2000)
  })
  onBeforeUnmount(()=>{if (revisitPoll) clearInterval(revisitPoll)})

  // Load tasks when the composable is first used in a component
  onMounted(fetchTasks)

  return {
    tasks,
    isLoading,
    error,
    fetchTasks,
    createTask,
    updateTask,
    removeTask,
    startTask,
    stopTask,
    stoppingTaskIds,
    startRevisit,
    pendingRevisitIds,
  }
}
