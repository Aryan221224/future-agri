// UI strings, in English. One table so the wording stays consistent across the app
// (the farmer's Today screen builds its sentences from these with {placeholders}).
type Dict = Record<string, string>

/** Dates and numbers are always formatted in English, whatever the browser's language. */
export const LOCALE = 'en'

const en: Dict = {
  'nav.farms': 'Farms', 'nav.mission': 'Mission', 'nav.twin': '3D farm', 'nav.drone': 'Drone twin',
  'nav.season': 'Season', 'nav.iterate': 'Iterations', 'nav.plan': 'Plan', 'nav.live': 'Live',
  'nav.records': 'Records', 'nav.aircraft': 'Aircraft', 'nav.system': 'System', 'nav.today': 'Today', 'nav.more': 'More',
  'mode.team': 'Team', 'mode.farmer': 'Farmer',
  'clock.day': 'Day {das} of {total}', 'clock.morning': 'morning', 'clock.flights': 'flights due',
  'clock.done': 'season complete', 'clock.waiting': 'waiting for data',
  'act.approve': 'Approve', 'act.defer': 'Defer', 'act.reject': 'Reject', 'act.fly': 'Fly now',
  'act.record': 'Record flight', 'act.close': 'Close', 'act.cancel': 'Cancel', 'act.save': 'Save',
  'act.retry': 'Try again', 'act.add_farm': 'Add a farm', 'act.open': 'Open',
  'product.herbicide': 'herbicide', 'product.fungicide': 'fungicide', 'product.scan': 'scan',
  'status.proposed': 'waiting for approval', 'status.approved': 'approved', 'status.flown': 'flown',
  'status.dropped': 'dropped', 'status.rejected': 'rejected',
  'mode.sim': 'Virtual test farm', 'mode.field': 'Live field',
  'offline': 'Offline: showing saved data', 'stale': 'Showing data from {time}; the latest update failed.',
  'today.title': 'Today on {farm}', 'today.day': '{date}, day {das} of the season',
  'today.spray_today': 'Spray {product} today',
  'today.spray_today.detail': '{targets_pct}% of the field. Dawn wind about {wind} m/s; {p}% of forecasts allow flying.',
  'today.wait_weather': 'Wait: the weather is not right for spraying',
  'today.wait_weather.detail': '{product} is due, but wind or rain is outside the safe limits today.',
  'today.wait_approval': 'Wait for the team to confirm',
  'today.wait_approval.detail': 'The twin recommends {product} from {due}. The operator will confirm.',
  'today.scan_due': 'A field scan is due',
  'today.scan_due.detail': 'Fly the camera drone over the field and upload the result.',
  'today.waiting_data': 'Waiting for new weather data',
  'today.waiting_data.detail': 'The latest weather is from {last}. Advice updates when new data arrives.',
  'today.no_action': 'No action needed today', 'today.no_action.detail': 'The crop is on track.',
  'today.season_over': 'The season is complete', 'today.season_over.detail': 'Harvest results are in the team view.',
  'today.next': 'Next: {product} around {due}', 'today.health': 'Field health', 'today.stage': 'Growth stage',
  'today.leaf_area': 'Leaf area index', 'today.alerts': 'Alerts', 'today.no_alerts': 'No alerts',
  'today.pick_farm': 'Choose your field', 'today.no_farms': 'No fields yet. Ask your operator to add your field.',
  'stage.emergence': 'Emerging', 'stage.vegetative': 'Growing leaves', 'stage.flowering': 'Flowering',
  'stage.filling': 'Filling', 'stage.mature': 'Mature',
  'today.outlook': 'Spray weather, next days', 'today.flyable': '{p}% of forecasts allow flying', 'today.no_outlook': 'No forecast available right now',
  'today.error': 'Could not load the advice', 'today.updated': 'Updated {time}', 'today.wind': 'wind {wind} m/s', 'today.rain': 'rain {rain} mm',
  'alert.proposal_waiting': '{product} proposed for {due}', 'alert.scan_due': 'Scan due {due}',
  'alert.spray_window': 'Likely spray window: {day}', 'alert.no_window': 'No spray window in the forecast',
  'alert.blight_risk': 'Blight risk: a fungicide is likely to pay off this week',
  'alert.weather_lag': 'Weather data is a few days behind', 'alert.device_silent': '{name} has not reported for a day',
  'alert.region_derogation': 'Aerial spraying needs a permit (derogation) here',
  'alert.region_unverified': 'Local drone rules are not verified: check before flying',
  'alert.session_unavailable': 'The mission has not started yet',
}

export function translate(key: string, params?: Record<string, unknown>): string {
  let s = en[key] ?? key
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      const val = typeof v === 'string' && en[`product.${v}`] ? en[`product.${v}`] : v
      s = s.replaceAll(`{${k}}`, val === null || val === undefined ? '–' : String(val))
    }
  }
  return s
}
