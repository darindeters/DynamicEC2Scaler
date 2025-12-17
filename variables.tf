variable "lambda_function_name" {
  description = "Name for the EC2 scaler Lambda function"
  type        = string
  default     = "DynamicEC2Scheduler"
}

variable "lambda_schedule_up_time" {
  description = "Cron expression that triggers the default scale up"
  type        = string
  default     = "cron(0 17 ? * MON-FRI *)"
}

variable "lambda_schedule_down_time" {
  description = "Cron expression that triggers the default scale down"
  type        = string
  default     = "cron(0 22 ? * MON-FRI *)"
}

variable "business_hours_schedule_up_time" {
  description = "Cron expression that triggers the business-hours scale up"
  type        = string
  default     = "cron(0 17 ? * MON-FRI *)"
}

variable "business_hours_schedule_down_time" {
  description = "Cron expression that triggers the business-hours scale down"
  type        = string
  default     = "cron(0 2 ? * TUE-SAT *)"
}

variable "default_downsize_type" {
  description = "Fallback instance type applied during scale-down when no tag override is present"
  type        = string
  default     = "t3.medium"
}

variable "batch_size" {
  description = "Number of instances to process per batch during scaling operations"
  type        = number
  default     = 10
}

variable "max_retries" {
  description = "Number of retry attempts for AWS API calls"
  type        = number
  default     = 3
}

variable "backoff_seconds" {
  description = "Base seconds to wait between retry attempts"
  type        = number
  default     = 5
}

variable "fail_fast" {
  description = "When true, aborts the batch on the first encountered error"
  type        = bool
  default     = false
}

variable "schedule_tag_key" {
  description = "Tag key used to assign instances to alternative scaling schedules"
  type        = string
  default     = "DynamicScalingSchedule"
}

variable "concurrent_instance_operations" {
  description = "Maximum number of EC2 instances to process concurrently"
  type        = number
  default     = 4
}

variable "savings_metric_namespace" {
  description = "CloudWatch metric namespace to use when publishing savings data"
  type        = string
  default     = "DynamicEC2Scaler/Savings"
}

variable "savings_plan_discount_percent" {
  description = "Percentage discount applied to On-Demand rates by an account-wide Compute Savings Plan"
  type        = number
  default     = 0
}

variable "savings_plan_discount_mode" {
  description = "Controls how Savings Plan discounts are applied"
  type        = string
  default     = "Manual"
  validation {
    condition     = contains(["Manual", "Coverage"], var.savings_plan_discount_mode)
    error_message = "savings_plan_discount_mode must be either Manual or Coverage"
  }
}

variable "savings_plan_coverage_lookback_days" {
  description = "Lookback window (in days) to use when deriving Savings Plan coverage data from Cost Explorer"
  type        = number
  default     = 30
}

variable "default_pricing_operating_system" {
  description = "Default operating system filter to use for pricing lookups when an instance platform cannot be detected"
  type        = string
  default     = "Linux"
  validation {
    condition     = contains(["Linux", "Windows", "RHEL", "SUSE"], var.default_pricing_operating_system)
    error_message = "default_pricing_operating_system must be one of Linux, Windows, RHEL, or SUSE"
  }
}

variable "default_pricing_license_model" {
  description = "Default license model filter to use for pricing lookups when an instance platform cannot be detected"
  type        = string
  default     = "No License required"
}

variable "default_pricing_preinstalled_software" {
  description = "Default pre-installed software filter to use for pricing lookups when an instance platform cannot be detected"
  type        = string
  default     = "NA"
}

variable "savings_log_bucket" {
  description = "Optional custom bucket name for storing savings and actuals logs (leave blank to auto-generate)"
  type        = string
  default     = ""
}
