import { useEffect, useRef } from 'react'

export interface GfEvent {
  entity: 'workflow' | 'model' | 'dataset' | 'run'
  id: number
}

export function useEvents(handler: (e: GfEvent) => void) {
  const ref = useRef(handler)
  ref.current = handler
  useEffect(() => {
    const es = new EventSource('/api/events')
    es.onmessage = (m) => ref.current(JSON.parse(m.data) as GfEvent)
    return () => es.close()
  }, [])
}
