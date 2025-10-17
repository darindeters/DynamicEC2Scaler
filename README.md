
# EC2 Dynamic Scheduler

This AWS CloudFormation stack resizes EC2 instances on a schedule to optimize compute costs for business-hours workloads. It scales instances down during off-hours and scales them back up before the next business day. This allows you to reduce spend without shutting down systems or triggering alerts from monitoring tools.

## 🔧 How It Works

- A Lambda function runs twice per day via EventBridge rules:
  - **Scale down at 7 PM Pacific (Standard Time)** — Implemented as `cron(0 3 ? * TUE-SAT *)`, which is 7 PM PT when UTC offset is −8. Switch to `cron(0 2 ? * TUE-SAT *)` during Daylight Saving Time or use the stack parameter to supply expressions for another timezone.
  - **Scale up at 4 AM Pacific (Mon–Fri, Standard Time)** — Implemented as `cron(0 12 ? * MON-FRI *)`. During Daylight Saving Time use `cron(0 11 ? * MON-FRI *)` to keep a 4 AM local start.
- Instances are rebooted once per resize operation (stop → modify → start)
- This works even if you're using Compute Savings Plans
- Minimal impact to existing tools, monitoring agents, or workflows
- Each scale-down run estimates the discounted hourly savings per instance (respecting any configured Compute Savings Plan discount), automatically matches Linux, Windows, and SQL Server license-included fleets to the right on-demand price, stores a JSON report in an S3 bucket for cost tracking, and emits the totals to a CloudWatch Metrics namespace so you can build dashboards or alarms. Each run now also stamps helper tags (`DynamicScalingLastScaleDownTimestamp`, `DynamicScalingLastScaleDownHourlySavings`) on the downsized instances so the next scale-up can measure real downtime.
- Scale-down summaries now include the projected off-hours duration, the projected total savings before the next scale-up, and publish additional CloudWatch metrics (`TotalProjectedOffHoursSavings`, `ProjectedOffHoursDurationHours`) that align with those projections.
- When the fleet scales back up, the function reads the saved metadata to calculate actual downtime per instance, writes an aggregated `actual-savings/` JSON report, publishes `TotalActualSavings`/`TotalActualDowntimeHours` metrics, and annotates each instance with `DynamicScalingLastScaleUpTimestamp` to avoid double-counting.

## 🏷️ Required EC2 Tags

Apply these tags to any EC2 instance you want managed by this scheduler:

| Tag Key                 | Tag Value            | Purpose                                           |
|------------------------|----------------------|---------------------------------------------------|
| `DynamicInstanceScaling` | `true`               | Opts the instance into scheduling                 |
| `PreferredInstanceType`  | `m7i.large` (example) | Specifies the instance type to return to at 4 AM |

> ⚠️ The instance type will be resized to `t3.medium` by default during off-hours.

### Optional Tags

| Tag Key                  | Example Value      | Purpose                                                                 |
|--------------------------|--------------------|-------------------------------------------------------------------------|
| `DynamicScalingSchedule` | `default`, `all`   | Assigns the instance to an alternate schedule. Comma-separated values allow an instance to opt into multiple schedules; instances without this tag use the default schedule. |

## 🔐 IAM Permissions

The Lambda function follows a least privilege model. It can only modify EC2 instances with the `DynamicInstanceScaling=true` tag. It also has scoped access to:

- Start/stop/modify EC2 instances
- Create EBS volume grants (for encrypted volumes)
- Write logs to CloudWatch Logs (14-day retention)
- Write savings reports to an S3 bucket created by the stack
- Query Cost Explorer Savings Plan coverage metrics when coverage-based discounts are enabled

## 📦 Deployment

You can deploy this stack using the AWS Console, AWS CLI, or SAM/CDK.

To deploy with AWS Console:

1. Download the CloudFormation template: [`ec2-dynamic-scheduler.yaml`](./ec2-dynamic-scheduler.yaml)
2. Upload it to CloudFormation and launch the stack
3. Apply the required tags to your EC2 instances

## 📝 Customization

- **Resize Target:** Control the off-hours instance type with the `OffHoursInstanceType` stack parameter (defaults to `t3.medium`).
- **Schedule:** Default cron expressions target Pacific working hours by converting the desired local times into UTC because `AWS::Events::Rule` does not currently support the `ScheduleExpressionTimezone` property. Update the `LambdaScheduleUpTime` and `LambdaScheduleDownTime` parameters to match your timezone or to account for Daylight Saving Time.
- **Multiple Schedules:** Use the `ScheduleTagKey` parameter (defaults to `DynamicScalingSchedule`) to choose which tag assigns instances to alternative schedules. Deploy additional EventBridge rules that invoke the Lambda with a different `schedule` payload (for example `"schedule": "team-b"`) and tag instances accordingly. A tag value of `all` opts an instance into every schedule.
- **Parallel Operations:** Control how many instances are processed simultaneously with the `ConcurrentInstanceOperations` parameter (defaults to 4). The Lambda now uses AWS waiters and polling instead of fixed sleeps, dramatically reducing idle time during stop/modify/start sequences.
- **Logging:** CloudWatch Log Group is created with 14-day retention. Logs show success and error messages per instance.
- **Savings Reports:** Every scale-down event writes a JSON summary to the provisioned S3 bucket (`SavingsLogBucket`) under `savings/<date>/<timestamp>.json`, which now captures projected downtime hours and projected total savings. Scale-up events complement this with measured results under `actual-savings/<date>/<timestamp>.json`, giving you both forecasted and realized savings without reprocessing the raw metrics. You can change the bucket properties or configure lifecycle rules by editing the CloudFormation template.
- **Savings Plan Discount:** Choose whether to provide a manual discount percentage (`SavingsPlanDiscountPercent`) or let the stack derive an effective rate from recent Cost Explorer coverage data by setting `SavingsPlanDiscountMode` to `Coverage`. Coverage mode uses the `ce:GetSavingsPlansCoverage` API (ensure Cost Explorer is enabled) and averages the last `SavingsPlanCoverageLookbackDays` (30 by default).
- **CloudWatch Metrics:** Use the `SavingsMetricNamespace` parameter to control where hourly savings metrics are published. These metrics expose the total run savings and per-instance estimates, enabling dashboards, anomaly detection, or cost alerts alongside the S3 JSON reports. Set the parameter to an empty string if you prefer to disable metric publication.
- **Pricing Detection:** The Lambda maps each instance's platform to the appropriate AWS Pricing filters before calculating savings. If an instance platform can't be detected, override the fallback filters with the `DefaultPricingOperatingSystem`, `DefaultPricingLicenseModel`, and `DefaultPricingPreInstalledSoftware` parameters instead of editing the function code.

## 🧪 Testing

To test in the Lambda console:

1. Open the Lambda function created by the stack
2. Create a test event using this JSON format:
```json
{
  "source": "Scheduled",
  "action": "scaleup"
}
```

## 🚀 Suggested Future Enhancements

If you are looking to extend the stack further, the following ideas can help deepen the savings insights or broaden operational coverage without forcing downstream customization in the Lambda code:

- **Rightsizing Recommendations:** Persist the observed instance hours and savings deltas to S3/CloudWatch and surface a daily or weekly summary that highlights candidates for permanent downsizing.
- **Notification Hooks:** Wire optional SNS/Slack notifications into the CloudFormation parameters so operations teams are alerted when a resize or savings report fails.
- **Override Schedules Per Tag:** Introduce additional opt-in tags (for example `DynamicScalingSchedule=weekends`) that map to distinct EventBridge cron expressions defined in the template.
- **Savings Dashboard Template:** Publish an optional CloudWatch dashboard resource that visualizes the emitted savings metrics out of the box.

These enhancements keep customization declarative by flowing new knobs through CloudFormation parameters instead of edits to the Lambda source.
