provider "aws" {
  region = "us-west-2"

  assume_role {
    role_arn = var.role_arn
  }
}

module "ec2_scaler" {
  source = "./modules/ec2-scaler"

  lambda_function_name                   = var.lambda_function_name
  lambda_schedule_up_time                = var.lambda_schedule_up_time
  lambda_schedule_down_time              = var.lambda_schedule_down_time
  business_hours_schedule_up_time        = var.business_hours_schedule_up_time
  business_hours_schedule_down_time      = var.business_hours_schedule_down_time
  default_downsize_type                  = var.default_downsize_type
  batch_size                             = var.batch_size
  max_retries                            = var.max_retries
  backoff_seconds                        = var.backoff_seconds
  fail_fast                              = var.fail_fast
  schedule_tag_key                       = var.schedule_tag_key
  concurrent_instance_operations         = var.concurrent_instance_operations
  savings_metric_namespace               = var.savings_metric_namespace
  default_pricing_operating_system       = var.default_pricing_operating_system
  default_pricing_license_model          = var.default_pricing_license_model
  default_pricing_preinstalled_software  = var.default_pricing_preinstalled_software
  savings_log_bucket                     = var.savings_log_bucket
  deployment_id                          = var.deployment_id
  existing_lambda_role_arn               = var.existing_lambda_role_arn
}

output "lambda_function_name" {
  value = module.ec2_scaler.lambda_function_name
}

output "savings_log_bucket_name" {
  value = module.ec2_scaler.savings_log_bucket_name
}
