"""
Curated architecture reference corpus for AVA.

These are paraphrased, compact knowledge chunks derived from official or primary
technical sources. They are written as reusable reference material for AVA's
architecture retrieval path and include source metadata so they can be tracked
and refreshed later.
"""

ARCHITECTURE_REFERENCE_DOCS = [
    {
        "title": "Kafka Design Overview",
        "category": "architecture_reference",
        "source": "Confluent Documentation",
        "source_url": "https://docs.confluent.io/platform/7.1/kafka/design.html",
        "summary": "Kafka is a distributed event streaming platform built for high-throughput, low-latency, fault-tolerant data pipelines. It organizes data into partitioned topics so producers and consumers can scale independently and consumers can replay durable event logs.",
        "architecture_notes": "Kafka is strongest when a system needs a durable event backbone between producers and consumers. It acts as the event transport and fan-out layer rather than as a database, and it is often paired with stream processors for aggregation and with serving stores for queryable views.",
        "request_flow": "Kafka usually does not sit directly in the synchronous request path. Services receive requests, commit primary business actions, then publish domain events into Kafka for downstream asynchronous processing.",
        "data_flow": "Producers write ordered events to partitions. Consumers read from those partitions independently, maintain offsets, and derive new feeds, aggregates, or downstream writes.",
        "tags": "architecture,kafka,event-streaming,distributed-systems,stream-processing"
    },
    {
        "title": "Introduction to Apache Kafka",
        "category": "architecture_reference",
        "source": "Confluent Documentation",
        "source_url": "https://docs.confluent.io/kafka/introduction.html",
        "summary": "Kafka is used to build real-time data pipelines and streaming applications. Producers write events to topics, topics are partitioned for parallelism, and replication is used for fault tolerance.",
        "architecture_notes": "Kafka works as a shared event log connecting loosely coupled services. Strong explanations call out producers, topics, partitions, consumers, and replayability.",
        "request_flow": "Client-facing services usually publish to Kafka after handling synchronous requests or internal state changes.",
        "data_flow": "Events move from producers to topics, then to one or more consumers that process, transform, or persist them.",
        "tags": "architecture,kafka,topics,partitions,consumers,producers"
    },
    {
        "title": "Event-Driven Architecture Style",
        "category": "architecture_reference",
        "source": "Azure Architecture Center",
        "source_url": "https://learn.microsoft.com/en-my/azure/architecture/guide/architecture-styles/event-driven",
        "summary": "An event-driven architecture consists of event producers, event channels or brokers, and event consumers. Producers and consumers are decoupled, events can be processed in near real time, and the style supports fan-out, replay, and independent scaling.",
        "architecture_notes": "This is a good grounding model for general event-driven systems. Publish-subscribe focuses on delivery to subscribers, while streaming uses a durable log that consumers can replay from arbitrary positions.",
        "request_flow": "If there is a synchronous request path, it usually triggers business actions that emit events rather than waiting for all downstream work to finish.",
        "data_flow": "Events flow from producers into a broker or ingestion layer and then to multiple consumers, often with independent state stores or processors on the consumer side.",
        "tags": "architecture,event-driven,pub-sub,event-streaming,consumers,producers"
    },
    {
        "title": "Architecture Styles Overview",
        "category": "architecture_reference",
        "source": "Azure Architecture Center",
        "source_url": "https://learn.microsoft.com/en-us/azure/architecture/guide/architecture-styles/",
        "summary": "Event-driven architectures excel when workloads require near-real-time processing, fault isolation, and scalable asynchronous communication through brokers or event channels.",
        "architecture_notes": "This style is chosen for loose coupling, elasticity, and the ability for multiple consumers to react independently to the same event stream.",
        "request_flow": "Request-serving components should remain narrow and defer cross-domain side effects into asynchronous consumers when possible.",
        "data_flow": "A broker or event ingestion component becomes the decoupling point between producers and downstream consumer capabilities.",
        "tags": "architecture,architecture-style,event-driven,scalability,fault-isolation"
    },
    {
        "title": "What Do You Mean by Event-Driven?",
        "category": "architecture_reference",
        "source": "Martin Fowler",
        "source_url": "https://martinfowler.com/articles/201701-event-driven.html",
        "summary": "Event-driven systems can use several patterns, including event notification, event-carried state transfer, event sourcing, and CQRS. Not every event-driven system uses all of them, and the term can describe different levels of event usage.",
        "architecture_notes": "Event-driven designs are not all identical. A good explanation identifies whether the events are simple notifications, durable state changes, or the source of truth for derived models.",
        "request_flow": "The synchronous path may only publish notifications, or it may publish events that become the authoritative history for the system.",
        "data_flow": "Consumers may only react to notifications, or they may rebuild views and projections from the event stream itself.",
        "tags": "architecture,event-driven,event-notification,event-sourcing,cqrs"
    },
    {
        "title": "Database Caching Strategies Using Redis",
        "category": "architecture_reference",
        "source": "AWS Whitepaper",
        "source_url": "https://docs.aws.amazon.com/whitepapers/latest/database-caching-strategies-using-redis/caching-patterns.html",
        "summary": "Common caching strategies include cache-aside (lazy loading) and write-through. In cache-aside, the application checks cache first and falls back to the database on misses, then populates the cache. In write-through, the cache is updated alongside the primary database write.",
        "architecture_notes": "Caches like Redis or EVCache act as latency and load-reduction layers in front of the durable store, not as the long-term system of record.",
        "request_flow": "Read requests check cache first, then query the primary store on cache miss.",
        "data_flow": "Writes go to the system of record and either invalidate or refresh cache entries depending on the chosen strategy.",
        "tags": "architecture,caching,redis,cache-aside,write-through,latency"
    },
    {
        "title": "Caching Best Practices",
        "category": "architecture_reference",
        "source": "AWS",
        "source_url": "https://aws.amazon.com/caching/implementation-considerations/",
        "summary": "Lazy caching or cache-aside is a common default because it only populates the cache for requested objects. Effective caching also depends on key design, TTL selection, and invalidation strategy.",
        "architecture_notes": "Cache efficiency depends on access patterns and freshness requirements. Caches are most useful for repeated, hot reads that would otherwise burden the backing store.",
        "request_flow": "Requests attempt a cache hit first for lower latency. Cache misses incur both cache lookup and backing-store access.",
        "data_flow": "The application controls cache population and invalidation based on read frequency, update patterns, and object freshness.",
        "tags": "architecture,caching,ttl,invalidation,cache-aside,read-scaling"
    },
    {
        "title": "Leveraging In-Memory Databases",
        "category": "architecture_reference",
        "source": "AWS Architecture Blog",
        "source_url": "https://aws.amazon.com/blogs/architecture/lets-architect-leveraging-in-memory-databases/",
        "summary": "In-memory databases help reduce backend strain, scale workloads, and support real-time or low-latency applications. The right caching or in-memory pattern depends on workload context rather than on a one-size-fits-all rule.",
        "architecture_notes": "A cache exists to serve hot reads, reduce latency, or protect the primary store. It should not be treated as interchangeable with a durable database.",
        "request_flow": "Performance-sensitive reads often terminate at the in-memory layer instead of the primary store.",
        "data_flow": "The in-memory layer may be populated lazily, write-through, or via background refresh depending on workload needs.",
        "tags": "architecture,caching,in-memory,performance,redis,serving-layer"
    },
    {
        "title": "Zuul as the Netflix Edge Service",
        "category": "architecture_reference",
        "source": "Netflix Zuul Wiki",
        "source_url": "https://github.com/Netflix/zuul/wiki",
        "summary": "Zuul is the front door for requests from devices and web sites to backend Netflix services. It is designed as an edge service for dynamic routing, monitoring, resiliency, and security.",
        "architecture_notes": "Zuul is the edge gateway in the synchronous request path. It is not the database or event backbone; it is the front-door routing and control layer.",
        "request_flow": "Requests enter through Zuul first. Zuul applies filters for auth, routing, resiliency, and traffic management before forwarding to backend origins or clusters.",
        "data_flow": "Zuul mostly governs request/response flow and edge telemetry rather than durable business-state flow.",
        "tags": "architecture,zuul,edge,gateway,request-routing,netflix"
    },
    {
        "title": "How Netflix Uses Zuul",
        "category": "architecture_reference",
        "source": "Netflix Zuul Wiki",
        "source_url": "https://github.com/Netflix/zuul/wiki/How-We-Use-Zuul-At-Netflix",
        "summary": "Netflix uses Zuul for edge routing, resiliency, software load balancing, monitoring, load shedding, stress testing, and multi-region traffic management.",
        "architecture_notes": "Zuul exists to provide routing, filtering, resiliency, and edge traffic control in a large-scale architecture rather than acting as a generic reverse proxy.",
        "request_flow": "Zuul sits at the edge, wraps outbound origin calls, can route to different backend clusters, and supports surgical routing and multi-region resiliency.",
        "data_flow": "Telemetry and routing decisions are generated at the edge while business data still flows through backend services and storage layers.",
        "tags": "architecture,zuul,netflix,edge-routing,resiliency,load-shedding"
    },
    {
        "title": "EVCache Overview",
        "category": "architecture_reference",
        "source": "Netflix EVCache Wiki",
        "source_url": "https://github.com/Netflix/EVCache/wiki/Overview",
        "summary": "EVCache is a distributed, replicated caching layer based on memcached semantics and designed for cloud deployments. It improves response time, reduces load on backend systems, and supports zone-aware reads with replicated writes.",
        "architecture_notes": "EVCache is a replicated cache for hot reads, not the system of record. A strong explanation calls out local-zone reads for latency and multi-zone replication for availability.",
        "request_flow": "Services read from the local-zone EVCache cluster first for low latency. On cache miss or fallback, they may read from another zone or from the source system.",
        "data_flow": "Writes are replicated across zones while reads prefer the local zone. This balances availability with lower read latency.",
        "tags": "architecture,evcache,cache,replication,zone-affinity,netflix"
    },
    {
        "title": "Mantis Infrastructure Overview",
        "category": "architecture_reference",
        "source": "Netflix Mantis Docs",
        "source_url": "https://netflix.github.io/mantis/internals/infrastructure-overview/",
        "summary": "Mantis consists of a control plane and a runtime plane. The control plane manages the lifecycle of jobs, and the runtime executes streaming jobs across the cluster.",
        "architecture_notes": "Mantis belongs in the observability or streaming analytics path rather than in the core request-serving path. It is a stream-processing and runtime platform, not the user-facing gateway.",
        "request_flow": "Mantis is usually not the direct ingress for end-user requests. Instead it receives operational or event streams from upstream systems.",
        "data_flow": "Streaming jobs run in the runtime plane while the control plane schedules and manages them. Mantis is well suited for monitoring, analytics, and real-time processing pipelines.",
        "tags": "architecture,mantis,stream-processing,observability,control-plane,netflix"
    },
    {
        "title": "Samza Architecture Layers",
        "category": "architecture_reference",
        "source": "Apache Samza Documentation",
        "source_url": "https://samza.apache.org/learn/documentation/0.14/introduction/architecture.html",
        "summary": "Samza has three layers: a streaming layer, an execution layer, and a processing layer. Kafka is commonly used as the streaming layer and a cluster manager such as YARN runs the processing tasks.",
        "architecture_notes": "Samza is the stream-processing layer that consumes from the event backbone, performs stateful processing or aggregation, and emits derived outputs. It is not the primary transport or serving database.",
        "request_flow": "Samza is typically downstream of user-facing request handling. It reacts to event streams rather than synchronous edge traffic.",
        "data_flow": "Events are read from Kafka or another stream, processed by Samza tasks, and then written to downstream stores, caches, or output streams.",
        "tags": "architecture,samza,stream-processing,kafka,pipeline,analytics"
    },
    {
        "title": "Apache Cassandra Overview",
        "category": "architecture_reference",
        "source": "Apache Cassandra Documentation",
        "source_url": "https://cassandra.apache.org/doc/latest/cassandra/architecture/overview.html",
        "summary": "Cassandra is a distributed NoSQL database designed for global availability, low-latency reads and writes, multi-primary replication, and scale-out on commodity hardware.",
        "architecture_notes": "Cassandra is the durable, distributed state store optimized for availability and scale. It usually sits in the data-serving layer and is often paired with caches for hot reads.",
        "request_flow": "Synchronous services may write primary state to Cassandra after request validation and business logic. Reads may go to Cassandra directly or through a cache-aside layer.",
        "data_flow": "Data is partitioned and replicated across nodes and often across datacenters. Downstream services and caches can consume derived state from services that write into Cassandra.",
        "tags": "architecture,cassandra,distributed-database,replication,availability,data-layer"
    },
]
