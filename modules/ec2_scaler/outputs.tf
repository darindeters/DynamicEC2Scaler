output "lambda_function_name" {
  value = aws_lambda_function.ec2_scaler.function_name
}

output "savings_log_bucket_name" {
  value = aws_s3_bucket.savings_log.bucket
}
