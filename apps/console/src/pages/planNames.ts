export const PLAN_NAMES: Record<string, string> = {
  none: 'No sprays (baseline)',
  calendar: 'Calendar schedule',
  twin_default_flight: 'Twin + default flight',
  twin_optimised_flight: 'Twin + optimised flight',
  twin_optimised_cheap_scan: 'Twin + optimised flight, cheaper scan',
  twin_optimised_early_scan: 'Twin + optimised flight, early scan',
  twin_alert_optimised_flight: 'Twin + blight alert + optimised flight',
}
export const planName = (p: string) => PLAN_NAMES[p] ?? p.replace(/_/g, ' ')
