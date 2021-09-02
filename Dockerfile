FROM ubuntu:focal as app

# System requirements.
RUN apt-get update && apt-get upgrade -qy
RUN apt-get install -qy \
	git-core \
	language-pack-en \
	python3.8 \
	python3-pip \
	python3.8-dev \
	libssl-dev
RUN pip3 install --upgrade pip setuptools
# delete apt package lists because we do not need them inflating our image
# RUN rm -rf /var/lib/apt/lists/*

# Python is Python3.
RUN ln -s /usr/bin/python3 /usr/bin/python

# Use UTF-8.
RUN locale-gen en_US.UTF-8
ENV LANG en_US.UTF-8
ENV LANGUAGE en_US:en
ENV LC_ALL en_US.UTF-8

RUN apt-get install -qy \
	curl \
	ca-certificates \
	gnupg \
;
RUN curl https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    | apt-key add -
RUN sh -c '\
    echo "deb http://apt.postgresql.org/pub/repos/apt focal-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list \
'
RUN apt update -qy
RUN DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tzdata
RUN apt install -qy postgresql-13 libpq-dev

RUN pip install tox

RUN mkdir -p /edx/app/openedx-webhooks
WORKDIR /edx/app/openedx-webhooks
COPY Makefile ./
COPY requirements ./requirements
RUN make install-dev-requirements

COPY . /edx/app/openedx-webhooks
# CMD ["make", "test"]
CMD ["py.test", "-rxefs", "--cov=openedx_webhooks", "--cov=tests", "--cov-report=", "--cov-context=test", "openedx_webhooks/github/dispatcher/actions/test/test_github_cla.py", "-k", "TestCla"]
