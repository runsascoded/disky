# The AWS Batch delete-job container (spec `specs/staged-delete.md` CP8): deletes
# a run's staged URIs and writes the result back to D1. Built + pushed to ECR;
# its URI is the `image` tfvar.
FROM amazon/aws-cli:latest
RUN yum install -y jq && yum clean all
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
