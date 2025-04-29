#!/usr/bin/env python
import uuid
import base64
from constructs import Construct
from cdktf import App, TerraformStack, TerraformOutput

# CDKTF AWS Provider constructs
from cdktf_cdktf_provider_aws.provider import AwsProvider
from cdktf_cdktf_provider_aws.data_aws_ami import DataAwsAmi
from cdktf_cdktf_provider_aws.vpc import Vpc
from cdktf_cdktf_provider_aws.subnet import Subnet
from cdktf_cdktf_provider_aws.internet_gateway import InternetGateway
from cdktf_cdktf_provider_aws.route_table import RouteTable
from cdktf_cdktf_provider_aws.route import Route
from cdktf_cdktf_provider_aws.route_table_association import RouteTableAssociation
from cdktf_cdktf_provider_aws.security_group import (
    SecurityGroup,
    SecurityGroupIngress,
    SecurityGroupEgress
)
from cdktf_cdktf_provider_aws.iam_role import IamRole
from cdktf_cdktf_provider_aws.iam_role_policy import IamRolePolicy
from cdktf_cdktf_provider_aws.iam_role_policy_attachment import IamRolePolicyAttachment
from cdktf_cdktf_provider_aws.iam_instance_profile import IamInstanceProfile
from cdktf_cdktf_provider_aws.lb import Lb
from cdktf_cdktf_provider_aws.lb_target_group import LbTargetGroup
from cdktf_cdktf_provider_aws.lb_listener import LbListener, LbListenerDefaultAction
from cdktf_cdktf_provider_aws.launch_template import LaunchTemplate
from cdktf_cdktf_provider_aws.autoscaling_group import AutoscalingGroup, AutoscalingGroupTag
from cdktf_cdktf_provider_aws.codedeploy_app import CodedeployApp
from cdktf_cdktf_provider_aws.codedeploy_deployment_group import CodedeployDeploymentGroup

# ─── Configuration Constants ────────────────────────────────────────────────────

REGION             = "ap-south-1"
VPC_CIDR           = "10.0.0.0/16"
AVAILABILITY_ZONES = ["ap-south-1a", "ap-south-1b"]
INSTANCE_TYPE      = "t3.micro"

# Raw bootstrap script for EC2 instances
USER_DATA = """#!/bin/bash
yum update -y && yum install -y ruby wget httpd
systemctl enable --now httpd
echo "<h1>Hello World V1 from $(hostname -f)</h1>" > /var/www/html/index.html
cd /home/ec2-user
wget https://aws-codedeploy-ap-south-1.s3.ap-south-1.amazonaws.com/latest/install
chmod +x ./install
./install auto
systemctl enable --now codedeploy-agent
mkdir -p /home/ec2-user/my-application
chown -R ec2-user:ec2-user /home/ec2-user
chmod 755 /home/ec2-user
"""

# Inline IAM policy documents
CODEDEPLOY_ASSUME_ROLE_POLICY = """{
  "Version":"2012-10-17",
  "Statement":[{
      "Action":"sts:AssumeRole",
      "Effect":"Allow",
      "Principal":{"Service":"codedeploy.amazonaws.com"}
  }]
}"""

EC2_ASSUME_ROLE_POLICY = """{
  "Version":"2012-10-17",
  "Statement":[{
      "Action":"sts:AssumeRole",
      "Effect":"Allow",
      "Principal":{"Service":"ec2.amazonaws.com"}
  }]
}"""

CODEDEPLOY_AUTOSCALING_POLICY = """{
  "Version":"2012-10-17",
  "Statement":[{
      "Effect":"Allow",
      "Action":[
          "autoscaling:*",
          "ec2:CreateTags",
          "ec2:RunInstances",
          "iam:PassRole"
      ],
      "Resource":"*"
  }]
}"""

EC2_S3_POLICY = """{
  "Version":"2012-10-17",
  "Statement":[{
      "Effect":"Allow",
      "Action":[
          "s3:GetObject",
          "s3:ListBucket"
      ],
      "Resource":[
          "arn:aws:s3:::python-demo-application-deployment-bucket",
          "arn:aws:s3:::python-demo-application-deployment-bucket/*"
      ]
  }]
}"""

# ─── Helper Functions ───────────────────────────────────────────────────────────

def create_resource_tags(base_tags: dict, name: str) -> dict:
    """Merge a common tags dictionary with a Name tag."""
    return {**base_tags, "Name": f"{name}"}

def create_target_group_config(name: str, unique: str, vpc_id: str, tags: dict) -> dict:
    """Build the kwargs for an Application Load Balancer target group."""
    return {
        "name":        f"{name}-tg-{unique}",
        "port":        80,
        "protocol":    "HTTP",
        "vpc_id":      vpc_id,
        "target_type": "instance",
        "health_check": {
            "enabled":             True,
            "interval":            30,
            "path":                "/",
            "port":                "traffic-port",
            "healthy_threshold":   2,
            "unhealthy_threshold": 2,
            "timeout":             5,
            "matcher":             "200"
        },
        "tags": create_resource_tags(tags, f"{name}-tg")
    }

# ─── Main Stack ────────────────────────────────────────────────────────────────

class MyStack(TerraformStack):
    """CDKTF stack for AWS Blue-Green Deployment infrastructure."""
    def __init__(self, scope: Construct, id: str):
        super().__init__(scope, id)

        # Unique suffix for resource names & base tags
        self.unique = uuid.uuid4().hex[:8]
        self.tags   = {
            "Project":     "BlueGreen",
            "Environment": "Production",
            "ManagedBy":   "CDKTF"
        }

        # 1) AWS provider
        AwsProvider(self, "AWS", region=REGION)

        # 2) Latest Amazon Linux 2 AMI (official owner "amazon")
        ami = DataAwsAmi(self, "amazon_linux_2",
            most_recent = True,
            owners      = ["amazon"],
            filter      = [
                {"name": "name",                "values": ["amzn2-ami-hvm-*-x86_64-gp2"]},
                {"name": "virtualization-type", "values": ["hvm"]},
                {"name": "root-device-type",    "values": ["ebs"]}
            ]
        )

        # 3) Base64-encode the user data once
        encoded_user_data = base64.b64encode(USER_DATA.encode("utf-8")).decode("utf-8")

        # 4) Networking
        vpc            = self._create_vpc()
        public_subnets = self._create_public_subnets(vpc)
        self._create_internet_gateway(vpc, public_subnets)

        # 5) Security Groups
        alb_sg, inst_sg = self._create_security_groups(vpc)

        # 6) IAM Roles & Instance Profile
        cd_role      = self._create_codedeploy_role()
        inst_profile = self._create_ec2_role_and_profile()

        # 7) ALB & Target Groups
        alb, blue_tg, green_tg = self._create_load_balancer(public_subnets, alb_sg, vpc)

        # 8) Auto Scaling Group with Base64-encoded user_data
        asg_blue = self._create_auto_scaling_group(
            inst_sg, inst_profile, blue_tg, public_subnets, ami.id, encoded_user_data
        )

        # 9) CodeDeploy
        self._create_codedeploy_resources(cd_role, blue_tg, green_tg, asg_blue)

        # 10) Outputs
        self._create_outputs(vpc, alb)


    # ─── Resources ──────────────────────────────────────────────────────────────

    def _create_vpc(self) -> Vpc:
        return Vpc(self, "vpc-main",
            cidr_block           = VPC_CIDR,
            enable_dns_hostnames = True,
            enable_dns_support   = True,
            instance_tenancy     = "default",
            tags                 = create_resource_tags(self.tags, "vpc")
        )

    def _create_public_subnets(self, vpc: Vpc) -> list[Subnet]:
        return [
            Subnet(self, f"subnet-public-{i+1}",
                vpc_id                  = vpc.id,
                cidr_block              = f"10.0.{i+1}.0/24",
                availability_zone       = az,
                map_public_ip_on_launch = True,
                tags                    = create_resource_tags(self.tags, f"public-subnet-{i+1}")
            )
            for i, az in enumerate(AVAILABILITY_ZONES)
        ]

    def _create_internet_gateway(self, vpc: Vpc, subnets: list[Subnet]) -> None:
        igw = InternetGateway(self, "igw-main",
            vpc_id = vpc.id,
            tags   = create_resource_tags(self.tags, "igw")
        )
        rt = RouteTable(self, "rt-public",
            vpc_id = vpc.id,
            tags   = create_resource_tags(self.tags, "public-rt")
        )
        Route(self, "internet-route",
            route_table_id         = rt.id,
            destination_cidr_block = "0.0.0.0/0",
            gateway_id             = igw.id
        )
        for i, subnet in enumerate(subnets):
            RouteTableAssociation(self, f"rt-assoc-public-{i+1}",
                subnet_id      = subnet.id,
                route_table_id = rt.id
            )

    def _create_security_groups(self, vpc: Vpc) -> tuple[SecurityGroup, SecurityGroup]:
        egress_all = [SecurityGroupEgress(
            description = "All outbound",
            from_port   = 0, to_port = 0,
            protocol    = "-1",
            cidr_blocks = ["0.0.0.0/0"]
        )]
        alb_sg = SecurityGroup(self, "sg-alb",
            name_prefix = f"alb-sg-{self.unique}-",
            vpc_id       = vpc.id,
            description  = "Allow HTTP to ALB",  # ASCII only
            ingress      = [SecurityGroupIngress(
                description = "HTTP from anywhere",
                from_port   = 80, to_port = 80,
                protocol    = "tcp",
                cidr_blocks = ["0.0.0.0/0"]
            )],
            egress       = egress_all,
            tags         = create_resource_tags(self.tags, "alb-sg")
        )
        inst_sg = SecurityGroup(self, "sg-instance",
            name_prefix = f"instance-sg-{self.unique}-",
            vpc_id       = vpc.id,
            description  = "Allow HTTP from ALB",
            ingress      = [SecurityGroupIngress(
                description     = "From ALB SG",
                from_port       = 80, to_port = 80,
                protocol        = "tcp",
                security_groups = [alb_sg.id]
            )],
            egress       = egress_all,
            tags         = create_resource_tags(self.tags, "instance-sg")
        )
        return alb_sg, inst_sg

    def _create_codedeploy_role(self) -> IamRole:
        role = IamRole(self, "role-codedeploy",
            name               = f"cd-role-{self.unique}",
            assume_role_policy = CODEDEPLOY_ASSUME_ROLE_POLICY,
            tags               = self.tags
        )
        IamRolePolicy(self, "codedeploy-autoscaling-policy",
            name   = "codedeploy-autoscaling",
            role   = role.id,
            policy = CODEDEPLOY_AUTOSCALING_POLICY
        )
        IamRolePolicyAttachment(self, "attach-cd-managed",
            role       = role.name,
            policy_arn = "arn:aws:iam::aws:policy/service-role/AWSCodeDeployRole"
        )
        return role

    def _create_ec2_role_and_profile(self) -> IamInstanceProfile:
        role = IamRole(self, "role-ec2",
            name               = f"ec2-role-{self.unique}",
            assume_role_policy = EC2_ASSUME_ROLE_POLICY,
            tags               = self.tags
        )
        IamRolePolicy(self, "ec2-s3-policy",
            name   = "ec2-s3-access",
            role   = role.id,
            policy = EC2_S3_POLICY
        )
        IamRolePolicyAttachment(self, "attach-ssm",
            role       = role.name,
            policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
        )
        return IamInstanceProfile(self, "profile-ec2",
            name = f"ec2-profile-{self.unique}",
            role = role.name
        )

    def _create_load_balancer(self, subnets: list[Subnet], alb_sg: SecurityGroup, vpc: Vpc):
        alb = Lb(self, "alb-app",
            name               = f"app-alb-{self.unique}",
            internal           = False,
            load_balancer_type = "application",
            security_groups    = [alb_sg.id],
            subnets            = [s.id for s in subnets],
            tags               = create_resource_tags(self.tags, "alb")
        )
        blue_tg = LbTargetGroup(self, "tg-blue", **create_target_group_config(
            "blue", self.unique, vpc.id, self.tags
        ))
        green_tg = LbTargetGroup(self, "tg-green", **create_target_group_config(
            "green", self.unique, vpc.id, self.tags
        ))
        LbListener(self, "listener-prod",
            load_balancer_arn = alb.arn,
            port              = 80,
            protocol          = "HTTP",
            default_action    = [LbListenerDefaultAction(
                type             = "forward",
                target_group_arn = blue_tg.arn
            )]
        )
        LbListener(self, "listener-test",
            load_balancer_arn = alb.arn,
            port              = 8080,
            protocol          = "HTTP",
            default_action    = [LbListenerDefaultAction(
                type             = "forward",
                target_group_arn = green_tg.arn
            )]
        )
        return alb, blue_tg, green_tg

    def _create_auto_scaling_group(
        self,
        inst_sg: SecurityGroup,
        inst_profile: IamInstanceProfile,
        blue_tg: LbTargetGroup,
        subnets: list[Subnet],
        ami_id: str,
        encoded_user_data: str
    ) -> AutoscalingGroup:
        lt = LaunchTemplate(self, "lt-app-blue",
            name_prefix            = f"app-lt-blue-{self.unique}-",
            image_id               = ami_id,
            instance_type          = INSTANCE_TYPE,
            vpc_security_group_ids = [inst_sg.id],
            iam_instance_profile   = {"name": inst_profile.name},
            user_data              = encoded_user_data,  # already Base64-encoded
            tags                   = create_resource_tags(self.tags, "lt-blue")
        )
        return AutoscalingGroup(self, "asg-blue",
            name                      = f"asg-blue-{self.unique}",
            desired_capacity          = 2,
            min_size                  = 1,
            max_size                  = 4,
            vpc_zone_identifier       = [s.id for s in subnets],
            target_group_arns         = [blue_tg.arn],
            health_check_type         = "ELB",
            health_check_grace_period = 300,
            launch_template           = {"id": lt.id, "version": "$Latest"},
            tag                       = [
                AutoscalingGroupTag(key="Name",        value="blue-server", propagate_at_launch=True),
                AutoscalingGroupTag(key="Environment", value="Production",      propagate_at_launch=True)
            ]
        )

    def _create_codedeploy_resources(
        self,
        cd_role: IamRole,
        blue_tg: LbTargetGroup,
        green_tg: LbTargetGroup,
        asg_blue: AutoscalingGroup
    ) -> None:
        app = CodedeployApp(self, "codedeploy-app",
            name             = f"app-{self.unique}",
            compute_platform = "Server",
            tags             = self.tags
        )
        CodedeployDeploymentGroup(self, "cd-deployment-group",
            app_name                       = app.name,
            deployment_group_name          = f"deploy-grp-{self.unique}",
            service_role_arn               = cd_role.arn,
            deployment_style               = {
                "deployment_option": "WITH_TRAFFIC_CONTROL",
                "deployment_type":   "BLUE_GREEN"
            },
            blue_green_deployment_config   = {
                "deployment_ready_option": {
                    "action_on_timeout": "CONTINUE_DEPLOYMENT"
                },
                "green_fleet_provisioning_option": {
                    "action": "COPY_AUTO_SCALING_GROUP"
                },
                "terminate_blue_instances_on_deployment_success": {
                    "action": "TERMINATE",
                    "termination_wait_time_in_minutes": 5
                }
            },
            auto_rollback_configuration     = {
                "enabled": True,
                "events":  ["DEPLOYMENT_FAILURE"]
            },
            load_balancer_info             = {
                "target_group_info": [
                    {"name": blue_tg.name},
                    {"name": green_tg.name}
                ]
            },
            autoscaling_groups             = [asg_blue.name],
            ec2_tag_set                    = [{
                "ec2_tag_filter": [{
                    "key":   "Environment",
                    "type":  "KEY_AND_VALUE",
                    "value": "Production"
                }]
            }],
            tags                            = self.tags
        )

    def _create_outputs(self, vpc: Vpc, alb: Lb):
        TerraformOutput(self, "vpc_id",
            value       = vpc.id,
            description = "VPC ID"
        )
        TerraformOutput(self, "alb_dns_name",
            value       = alb.dns_name,
            description = "ALB DNS name"
        )


# ─── Entrypoint ───────────────────────────────────────────────────────────────

def main():
    app = App()
    MyStack(app, "BG-deployment")
    app.synth()

if __name__ == "__main__":
    main()