
# EC2 Dynamic Scheduler

This AWS CloudFormation stack resizes EC2 instances on a schedule to optimize compute costs for business-hours workloads. It scales instances down during off-hours and scales them back up before the next business day. This allows you to reduce spend without shutting down systems or triggering alerts from monitoring tools.

## 🔧 How It Works

- A Lambda function runs twice per day via EventBridge rules:
  - **7 PM Pacific** — Scales down tagged instances to a smaller type (e.g. `t3.medium`)
  - **4 AM Pacific (Mon–Fri only)** — Scales them back up to the original size
- Instances are rebooted once per resize operation (stop → modify → start)
- This works even if you're using Compute Savings Plans
- Minimal impact to existing tools, monitoring agents, or workflows

## 🏷️ Required EC2 Tags

Apply these tags to any EC2 instance you want managed by this scheduler:

| Tag Key                 | Tag Value            | Purpose                                           |
|------------------------|----------------------|---------------------------------------------------|
| `DynamicInstanceScaling` | `true`               | Opts the instance into scheduling                 |
| `PreferredInstanceType`  | `m7i.large` (example) | Specifies the instance type to return to at 4 AM |

> ⚠️ The instance type will be resized to `t3.medium` by default during off-hours.

## 🔐 IAM Permissions

The Lambda function follows a least privilege model. It can only modify EC2 instances with the `DynamicInstanceScaling=true` tag. It also has scoped access to:

- Start/stop/modify EC2 instances
- Create EBS volume grants (for encrypted volumes)
- Write logs to CloudWatch Logs (14-day retention)

## 📦 Deployment

You can deploy this stack using the AWS Console, AWS CLI, or SAM/CDK.

To deploy with AWS Console:

1. Download the CloudFormation template: [`ec2-dynamic-scheduler.yaml`](./ec2-dynamic-scheduler.yaml)
2. Upload it to CloudFormation and launch the stack
3. Apply the required tags to your EC2 instances

## 📝 Customization

- **Resize Target:** The off-hours instance type defaults to `t3.medium`. You can change this in the Lambda code.
- **Schedule:** Default schedule is hardcoded for Pacific Time. You can update the EventBridge cron rules if needed.
- **Logging:** CloudWatch Log Group is created with 14-day retention. Logs show success and error messages per instance.

## 🧪 Testing

To test in the Lambda console:

1. Open the Lambda function created by the stack
2. Create a test event using this JSON format:
```json
{
  "source": "Scheduled",
  "action": "scaleup"
}
