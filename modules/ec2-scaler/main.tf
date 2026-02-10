data "aws_partition" "current" {}

data "archive_file" "lambda" {
  type        = "zip"
  source_file = "${path.root}/lambda/index.py"
  output_path = "${path.module}/lambda.zip"
}


data "aws_caller_identity" "current" {}

resource "random_id" "deployment" {
  byte_length = 4

  keepers = {
    lambda_function_name = var.lambda_function_name
    deployment_id        = trimspace(var.deployment_id)
  }
}

locals {
  effective_deployment_id = trimspace(var.deployment_id) != "" ? trimspace(var.deployment_id) : random_id.deployment.hex
  role_name               = "EC2ScalerLambdaRole-${local.effective_deployment_id}"
  policy_name             = "EC2ScalerPolicy-${local.effective_deployment_id}"
  generated_bucket_name   = lower("infrastudent-savings-${data.aws_caller_identity.current.account_id}-${local.effective_deployment_id}")
  savings_bucket_name     = trimspace(var.savings_log_bucket) != "" ? trimspace(var.savings_log_bucket) : local.generated_bucket_name
  lambda_role_arn         = trimspace(var.existing_lambda_role_arn) != "" ? trimspace(var.existing_lambda_role_arn) : aws_iam_role.lambda[0].arn
}

resource "aws_s3_bucket" "savings_log" {
  bucket = local.savings_bucket_name
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.lambda_function_name}-${local.effective_deployment_id}"
  retention_in_days = 14
}

resource "aws_iam_role" "lambda" {
  count = trimspace(var.existing_lambda_role_arn) == "" ? 1 : 0
  name  = local.role_name

  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  count = trimspace(var.existing_lambda_role_arn) == "" ? 1 : 0
  name  = local.policy_name
  role  = aws_iam_role.lambda[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = [
          "ec2:DescribeInstances",
          "ec2:DescribeTags",
          "pricing:GetProducts",
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "kms:CreateGrant"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["ec2:StopInstances", "ec2:StartInstances", "ec2:ModifyInstanceAttribute", "ec2:CreateTags"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "ec2:ResourceTag/DynamicInstanceScaling" = "true"
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "arn:${data.aws_partition.current.partition}:s3:::${aws_s3_bucket.savings_log.bucket}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ce:GetSavingsPlansCoverage"]
        Resource = "*"
      }
    ]
  })
}


resource "aws_ssm_document" "on_demand_scaling" {
  name            = "${var.lambda_function_name}-${local.effective_deployment_id}-OnDemandScaling"
  document_type   = "Automation"
  document_format = "YAML"

  content = yamlencode({
    schemaVersion = "0.3"
    description = <<-EOT
      ## EC2 ON-DEMAND SCALING AUTOMATION

      Manually trigger scale up or scale down actions for EC2 instances opted into the DynamicEC2Scheduler.

      ### REQUIRED EC2 INSTANCE TAG

      **DynamicInstanceScaling** = `true`

      * PreferredInstanceType tag is automatically added when an instance is first scaled down.

      ### BEHAVIOR

      * **Scale Down:** Resizes instances to t3.medium
      * **Scale Up:** Restores instances to their PreferredInstanceType
    EOT
    parameters = {
      Action = {
        type          = "String"
        allowedValues = ["scaleup", "scaledown"]
        default       = "scaleup"
        description   = "scaleup = Restore to PreferredInstanceType\nscaledown = Resize to t3.medium"
      }
      Schedule = {
        type          = "String"
        allowedValues = ["default", "business-hours", "all"]
        default       = "default"
        description   = "default = Instances tagged with default schedule\nbusiness-hours = Instances tagged with business-hours schedule\nall = All opted-in instances"
      }
      EnvironmentCategory = {
        type        = "String"
        default     = "all"
        description = "Optional environmentCategory tag filter. all = no filtering; any other value filters to matching environmentCategory tag."
      }
      AppName = {
        type        = "String"
        default     = "all"
        description = "Optional appName tag filter. all = no filtering; any other value filters to matching appName tag."
      }
    }
    mainSteps = [
      {
        name           = "InvokeLambda"
        action         = "aws:invokeLambdaFunction"
        timeoutSeconds = 900
        onFailure      = "Abort"
        inputs = {
          FunctionName = aws_lambda_function.ec2_scaler.function_name
          InputPayload = {
            source              = "Scheduled"
            action              = "{{Action}}"
            schedule            = "{{Schedule}}"
            environmentCategory = "{{EnvironmentCategory}}"
            appName             = "{{AppName}}"
          }
        }
        outputs = [
          {
            Name     = "ProcessedInstances"
            Selector = "$.Payload.processed_instances"
            Type     = "Integer"
          },
          {
            Name     = "SkippedInstances"
            Selector = "$.Payload.skipped_instances"
            Type     = "Integer"
          },
          {
            Name     = "Action"
            Selector = "$.Payload.action"
            Type     = "String"
          },
          {
            Name     = "Schedule"
            Selector = "$.Payload.schedule"
            Type     = "String"
          },
          {
            Name     = "EnvironmentCategory"
            Selector = "$.Payload.environment_category"
            Type     = "String"
          },
          {
            Name     = "AppName"
            Selector = "$.Payload.app_name"
            Type     = "String"
          },
          {
            Name     = "LambdaStatusCode"
            Selector = "$.StatusCode"
            Type     = "Integer"
          }
        ]
      }
    ]
  })
}

resource "aws_lambda_function" "ec2_scaler" {
  function_name = "${var.lambda_function_name}-${local.effective_deployment_id}"
  description   = "Scales EC2 instances up or down based on schedule and tags"
  runtime       = "python3.12"
  role          = local.lambda_role_arn
  handler       = "index.lambda_handler"
  memory_size   = 512
  timeout       = 300

  reserved_concurrent_executions = 10

  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = {
      BATCH_SIZE                            = var.batch_size
      DEFAULT_DOWNSIZE_TYPE                 = var.default_downsize_type
      MAX_RETRIES                           = var.max_retries
      BACKOFF_SECS                          = var.backoff_seconds
      FAIL_FAST                             = tostring(var.fail_fast)
      SAVINGS_BUCKET                        = aws_s3_bucket.savings_log.bucket
      SAVINGS_METRIC_NAMESPACE              = var.savings_metric_namespace
      DEFAULT_PRICING_OPERATING_SYSTEM      = var.default_pricing_operating_system
      DEFAULT_PRICING_LICENSE_MODEL         = var.default_pricing_license_model
      DEFAULT_PRICING_PREINSTALLED_SOFTWARE = var.default_pricing_preinstalled_software
      SCALE_UP_CRON_EXPRESSION              = var.lambda_schedule_up_time
      SCALE_DOWN_CRON_EXPRESSION            = var.lambda_schedule_down_time
      SCHEDULE_TAG_KEY                      = var.schedule_tag_key
      MAX_CONCURRENT_OPERATIONS             = var.concurrent_instance_operations
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_cloudwatch_event_rule" "default_down" {
  name                = "EC2ScalerScheduleDown-${local.effective_deployment_id}"
  description         = "Triggers Lambda to scale down EC2 instances at 7 PM Pacific, Monday through Friday"
  schedule_expression = var.lambda_schedule_down_time
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_rule" "default_up" {
  name                = "EC2ScalerScheduleUp-${local.effective_deployment_id}"
  description         = "Triggers Lambda to scale up EC2 instances at 4 AM Pacific, Monday through Friday"
  schedule_expression = var.lambda_schedule_up_time
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_rule" "business_down" {
  name                = "EC2ScalerBusinessHoursScheduleDown-${local.effective_deployment_id}"
  description         = "Triggers Lambda to scale down EC2 instances at 6 PM Pacific, Monday through Friday"
  schedule_expression = var.business_hours_schedule_down_time
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_rule" "business_up" {
  name                = "EC2ScalerBusinessHoursScheduleUp-${local.effective_deployment_id}"
  description         = "Triggers Lambda to scale up EC2 instances at 9 AM Pacific, Monday through Friday"
  schedule_expression = var.business_hours_schedule_up_time
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_target" "default_down" {
  rule      = aws_cloudwatch_event_rule.default_down.name
  target_id = "DownTarget-${local.effective_deployment_id}"
  arn       = aws_lambda_function.ec2_scaler.arn
  input = jsonencode({
    action   = "scaledown"
    source   = "eventbridge"
    schedule = "default"
  })
}

resource "aws_cloudwatch_event_target" "default_up" {
  rule      = aws_cloudwatch_event_rule.default_up.name
  target_id = "UpTarget-${local.effective_deployment_id}"
  arn       = aws_lambda_function.ec2_scaler.arn
  input = jsonencode({
    action   = "scaleup"
    source   = "eventbridge"
    schedule = "default"
  })
}

resource "aws_cloudwatch_event_target" "business_down" {
  rule      = aws_cloudwatch_event_rule.business_down.name
  target_id = "BusinessHoursDownTarget-${local.effective_deployment_id}"
  arn       = aws_lambda_function.ec2_scaler.arn
  input = jsonencode({
    action   = "scaledown"
    source   = "eventbridge"
    schedule = "business-hours"
  })
}

resource "aws_cloudwatch_event_target" "business_up" {
  rule      = aws_cloudwatch_event_rule.business_up.name
  target_id = "BusinessHoursUpTarget-${local.effective_deployment_id}"
  arn       = aws_lambda_function.ec2_scaler.arn
  input = jsonencode({
    action   = "scaleup"
    source   = "eventbridge"
    schedule = "business-hours"
  })
}

resource "aws_lambda_permission" "default_down" {
  statement_id  = "AllowExecutionFromEventBridgeDown-${local.effective_deployment_id}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ec2_scaler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.default_down.arn
}

resource "aws_lambda_permission" "default_up" {
  statement_id  = "AllowExecutionFromEventBridgeUp-${local.effective_deployment_id}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ec2_scaler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.default_up.arn
}

resource "aws_lambda_permission" "business_down" {
  statement_id  = "AllowExecutionFromBusinessHoursDown-${local.effective_deployment_id}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ec2_scaler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.business_down.arn
}

resource "aws_lambda_permission" "business_up" {
  statement_id  = "AllowExecutionFromBusinessHoursUp-${local.effective_deployment_id}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ec2_scaler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.business_up.arn
}
