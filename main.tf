terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
  }
}

provider "aws" {}

data "aws_partition" "current" {}

data "archive_file" "lambda" {
  type        = "zip"
  source_file = "lambda/index.py"
  output_path = "${path.module}/lambda.zip"
}

resource "aws_s3_bucket" "savings_log" {
  bucket = var.savings_log_bucket != "" ? var.savings_log_bucket : null
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.lambda_function_name}"
  retention_in_days = 14
}

resource "aws_iam_role" "lambda" {
  name = "EC2ScalerLambdaRole"

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
  name = "EC2ScalerPolicy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances", "ec2:DescribeTags", "pricing:GetProducts", "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "kms:CreateGrant"]
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

resource "aws_lambda_function" "ec2_scaler" {
  function_name = var.lambda_function_name
  description   = "Scales EC2 instances up or down based on schedule and tags"
  runtime       = "python3.12"
  role          = aws_iam_role.lambda.arn
  handler       = "index.lambda_handler"
  memory_size   = 512
  timeout       = 300

  reserved_concurrent_executions = 10

  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = {
      BATCH_SIZE                              = var.batch_size
      DEFAULT_DOWNSIZE_TYPE                   = var.default_downsize_type
      MAX_RETRIES                             = var.max_retries
      BACKOFF_SECS                            = var.backoff_seconds
      FAIL_FAST                               = tostring(var.fail_fast)
      SAVINGS_BUCKET                          = aws_s3_bucket.savings_log.bucket
      SAVINGS_METRIC_NAMESPACE                = var.savings_metric_namespace
      DEFAULT_PRICING_OPERATING_SYSTEM        = var.default_pricing_operating_system
      DEFAULT_PRICING_LICENSE_MODEL           = var.default_pricing_license_model
      DEFAULT_PRICING_PREINSTALLED_SOFTWARE   = var.default_pricing_preinstalled_software
      SCALE_UP_CRON_EXPRESSION                = var.lambda_schedule_up_time
      SCALE_DOWN_CRON_EXPRESSION              = var.lambda_schedule_down_time
      SCHEDULE_TAG_KEY                        = var.schedule_tag_key
      MAX_CONCURRENT_OPERATIONS               = var.concurrent_instance_operations
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_cloudwatch_event_rule" "default_down" {
  name                = "EC2ScalerScheduleDown"
  description         = "Triggers Lambda to scale down EC2 instances at 7 PM Pacific, Monday through Friday"
  schedule_expression = var.lambda_schedule_down_time
  is_enabled          = true
}

resource "aws_cloudwatch_event_rule" "default_up" {
  name                = "EC2ScalerScheduleUp"
  description         = "Triggers Lambda to scale up EC2 instances at 4 AM Pacific, Monday through Friday"
  schedule_expression = var.lambda_schedule_up_time
  is_enabled          = true
}

resource "aws_cloudwatch_event_rule" "business_down" {
  name                = "EC2ScalerBusinessHoursScheduleDown"
  description         = "Triggers Lambda to scale down EC2 instances at 6 PM Pacific, Monday through Friday"
  schedule_expression = var.business_hours_schedule_down_time
  is_enabled          = true
}

resource "aws_cloudwatch_event_rule" "business_up" {
  name                = "EC2ScalerBusinessHoursScheduleUp"
  description         = "Triggers Lambda to scale up EC2 instances at 9 AM Pacific, Monday through Friday"
  schedule_expression = var.business_hours_schedule_up_time
  is_enabled          = true
}

resource "aws_cloudwatch_event_target" "default_down" {
  rule      = aws_cloudwatch_event_rule.default_down.name
  target_id = "DownTarget"
  arn       = aws_lambda_function.ec2_scaler.arn
  input     = '{"action": "scaledown", "source": "eventbridge", "schedule": "default"}'
}

resource "aws_cloudwatch_event_target" "default_up" {
  rule      = aws_cloudwatch_event_rule.default_up.name
  target_id = "UpTarget"
  arn       = aws_lambda_function.ec2_scaler.arn
  input     = '{"action": "scaleup", "source": "eventbridge", "schedule": "default"}'
}

resource "aws_cloudwatch_event_target" "business_down" {
  rule      = aws_cloudwatch_event_rule.business_down.name
  target_id = "BusinessHoursDownTarget"
  arn       = aws_lambda_function.ec2_scaler.arn
  input     = '{"action": "scaledown", "source": "eventbridge", "schedule": "business-hours"}'
}

resource "aws_cloudwatch_event_target" "business_up" {
  rule      = aws_cloudwatch_event_rule.business_up.name
  target_id = "BusinessHoursUpTarget"
  arn       = aws_lambda_function.ec2_scaler.arn
  input     = '{"action": "scaleup", "source": "eventbridge", "schedule": "business-hours"}'
}

resource "aws_lambda_permission" "default_down" {
  statement_id  = "AllowExecutionFromEventBridgeDown"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ec2_scaler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.default_down.arn
}

resource "aws_lambda_permission" "default_up" {
  statement_id  = "AllowExecutionFromEventBridgeUp"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ec2_scaler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.default_up.arn
}

resource "aws_lambda_permission" "business_down" {
  statement_id  = "AllowExecutionFromBusinessHoursDown"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ec2_scaler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.business_down.arn
}

resource "aws_lambda_permission" "business_up" {
  statement_id  = "AllowExecutionFromBusinessHoursUp"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ec2_scaler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.business_up.arn
}

output "lambda_function_name" {
  value = aws_lambda_function.ec2_scaler.function_name
}

output "savings_log_bucket_name" {
  value = aws_s3_bucket.savings_log.bucket
}
