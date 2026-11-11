import { SessionLogSwitch as SharedSwitch } from '@open-athena/auth/react/session-log'
import '@open-athena/auth/react/session-log.css'
import { Link } from 'react-router-dom'
export { SessionLogSwitchView, switchStatus, type SessionLogSwitchViewProps as SwitchViewProps, type SwitchEnd as End } from '@open-athena/auth/react/session-log'

export function SessionLogSwitch() {
  return <SharedSwitch description={<>
    Records viewer actions for <Link to="/admin/sessions">session replay</Link> and the private{' '}
    <Link to="/admin/activity">activity feed</Link>. <Link to="/privacy">Privacy details</Link>.
  </>} />
}
