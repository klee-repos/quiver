output "instance_id" {
  value       = aws_instance.quiver.id
  description = "EC2 instance running the headless trader"
}

output "public_ip" {
  value       = aws_instance.quiver.public_ip
  description = "SSH here for the one-time Robinhood MCP auth"
}

output "alerts_topic_arn" {
  value       = aws_sns_topic.alerts.arn
  description = "Confirm the email subscription to receive alerts"
}

output "log_group" {
  value       = aws_cloudwatch_log_group.quiver.name
  description = "CloudWatch Logs group for tick output"
}
