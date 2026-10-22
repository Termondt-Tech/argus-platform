# Trading System Architecture

## 0DTE SPX Specific Trading System Best Practices

To transition from standard enterprise software to a robust 0DTE SPX execution engine, you must implement domain-specific architectural patterns focused on risk, market microstructure, and state resilience. In Argus top priority is following Trading System Best Practices.

1. Market Microstructure and Order Execution

⚬ Adaptive Limit Order Algorithms: Never use market orders for 0DTE SPX options. They suffer from wide bid-ask spreads, especially during high volatility or near the open/close. Your system must use adaptive routing (e.g., starting at the mid-price and stepping by $0.05 increments toward the natural ask/bid over a 30-second window) to mitigate slippage.
⚬ Cash-Settled Mechanics: SPX is a European-style, cash-settled index, meaning there is no early assignment risk and no underlying shares to manage at expiration. Your system architecture does not need to handle equity pinning or share-assignment logic, but it must account for exact AM vs. PM settlement calculations depending on the specific SPX chain being traded.
⚬ Smart Cancel/Replace Logic: Track the time-in-force of pending orders. If the market moves away rapidly, the system must recognize stale limit prices, cancel the existing order, and recalculate a new pricing threshold before submitting a replacement, avoiding getting "run over" by momentum.

2. Asynchronous State and Broker Synchronization

⚬ Idempotency in Order Routing: Network timeouts happen. If your system loses connection immediately after sending an order, it must not blindly resend the order upon reconnection, which could result in double sizing. Every order must carry a unique, idempotent Client Order ID (UUID) that the broker recognizes to prevent duplicate fills.
⚬ Reconciliation State Machine: The system must run a continuous reconciliation loop that compares your local, in-memory assumption of open positions against the broker's API truth. If a discrepancy is detected (e.g., a partial fill wasn't broadcasted via websocket), the system should halt trading and alert you immediately.

3. Hardcoded Risk and Capital Management

⚬ Deterministic Circuit Breakers: Implement absolute maximum daily drawdown limits at the application layer, completely decoupled from the strategy logic. If the account loses a specified dollar amount in a single session, a globally scoped "Kill Switch" must fire, liquidating all open SPX positions and locking out new entries for the rest of the day.
⚬ Gamma and Delta Exposure Caps: 0DTE options experience explosive gamma risk (rapid delta changes as expiration approaches). Position sizing cannot just be based on the "number of contracts." The system must calculate real-time portfolio net delta and gamma, rejecting new trade signals if Greeks breach strict risk thresholds.

4. Data Parity and Backtesting Fidelity

⚬ Slippage and Spread Modeling: When backtesting your system, avoid lookahead bias and model realistic slippage. Filling backtests at the historical mid-price is unrealistic for SPX 0DTE. Your backtester must penalize trades by assuming fills closer to the bid/ask natural, mapping directly to how your live limit-order execution engine behaves.
⚬ Time Synchronization: Execute trades using high-precision timestamps synchronized via NTP. A latency of a few seconds can turn a winning 0DTE trade into a loser, so accurate timestamping is critical for aligning strategy signals with live market execution.

## General System Design and Development Principles and Best Practices

In Argus, second priority is General System Design and Development Principles and Best Practices.

1. SOLID Principles SOLID is an acronym that represents five design principles intended to make software designs more understandable, flexible, and maintainable. These principles apply primarily to object-oriented programming:

S - Single Responsibility Principle (SRP): A class should have one, and only one, reason to change. It ensures that a class has a single responsibility, making it easier to maintain and test.
O - Open/Closed Principle (OCP): Software entities (classes, modules, functions) should be open for extension but closed for modification. This means you can add new functionality without altering existing code, thus reducing the risk of introducing bugs.
L - Liskov Substitution Principle (LSP): Objects of a superclass should be replaceable with objects of a subclass without affecting the correctness of the program. This principle ensures that a derived class should extend the base class without changing its behavior.
I - Interface Segregation Principle (ISP): Clients should not be forced to depend on interfaces they do not use. This encourages the creation of smaller, more specific interfaces rather than a large general-purpose one.
D - Dependency Inversion Principle (DIP): High-level modules should not depend on low-level modules. Both should depend on abstractions (e.g., interfaces). This principle aims to decouple code and reduce tight dependencies between classes.
2. The Twelve-Factor App The Twelve-Factor App is a methodology for building scalable, maintainable web applications, especially suited for modern cloud-native applications. It ensures consistency, portability, and ease of deployment. The twelve factors are:

Codebase: Use a single codebase tracked in version control, many deploys.
Dependencies: Explicitly declare and isolate dependencies via a dependency management tool (e.g., package.json, pip).
Config: Store configuration in the environment, not in the code.
Backing Services: Treat backing services (databases, queues, caches) as attached resources, accessed via URLs or credentials.
Build, Release, Run: Strictly separate the build and run stages; builds are packaged and immutable, and releases combine builds with environment configuration.
Processes: Execute the app as one or more stateless processes; any needed persistence should be in a database or other external service.
Port Binding: Export services via port binding to make them self-contained.
Concurrency: Scale out via the process model, allowing multiple instances of the app to run concurrently.
Disposability: Maximize robustness by ensuring fast startup and graceful shutdown.
Dev/Prod Parity: Keep development, staging, and production environments as similar as possible.
Logs: Treat logs as event streams, outputting them in a consistent format for centralized aggregation.
Admin Processes: Run administrative tasks (migrations, scripts) as one-off processes in the same environment as the app.
3. DRY (Don’t Repeat Yourself) DRY emphasizes reducing repetition of code or logic in software development. Repeating logic can make code harder to maintain, error-prone, and difficult to update. Using abstractions like functions, classes, and modules helps in adhering to the DRY principle.

4. YAGNI (You Aren’t Gonna Need It) YAGNI advocates for implementing only what is needed at the moment. Avoid adding functionality or complexity unless there’s a present requirement. This principle minimizes wasted effort and unnecessary code.

5. KISS (Keep It Simple, Stupid) The KISS principle highlights the importance of simplicity in design. Complex systems are harder to debug and maintain. Keeping designs simple and modular makes the software easier to understand and extend.

6. Loose Coupling and High Cohesion Loose Coupling: Components should be minimally dependent on each other, allowing for easier testing, maintenance, and refactoring. High Cohesion: Classes or modules should have a well-defined purpose and should contain code related to that purpose.

7. Separation of Concerns Separation of Concerns divides a system into distinct sections, each responsible for a single aspect of the functionality. For example, in MVC (Model-View-Controller), the model deals with data, the view handles the UI, and the controller manages input.

8. Test-Driven Development (TDD) In TDD, you write tests before writing the code that fulfills the tests. This approach ensures that your code is built with testing in mind, making it more reliable and easier to refactor.

9. Continuous Integration and Continuous Deployment (CI/CD) CI/CD automates the process of code integration, testing, and deployment. It ensures that code is always in a deployable state and encourages frequent, small releases that minimize bugs and issues.

10. Event-Driven Architecture Event-driven systems decouple components using events. In an event-driven system, components emit events, which other components subscribe to and react to. This pattern is particularly useful for scalable, decoupled microservices architectures.

11. Hexagonal Architecture (Ports and Adapters) Hexagonal Architecture separates the core business logic (domain) from external systems (like databases, user interfaces, or other services). The idea is to keep the application core agnostic to the technical infrastructure, improving maintainability and testability.

12. Microservices Architecture In a microservices architecture, the application is composed of small, loosely coupled services, each responsible for a specific function. Each service can be developed, deployed, and scaled independently, improving agility and fault isolation.

13. Domain-Driven Design (DDD) DDD emphasizes modeling software based on the business domain, ensuring that the system reflects real-world problems and solutions. It uses concepts like entities, value objects, aggregates, repositories, and bounded contexts to structure the system.

14. CQRS (Command Query Responsibility Segregation) CQRS separates commands (actions that change the system’s state) from queries (actions that retrieve data). This pattern is often used with event sourcing to provide a scalable, flexible architecture.

15. Infrastructure as Code (IaC) IaC is the practice of managing and provisioning computing infrastructure through machine-readable files, rather than manual hardware configuration. Tools like Terraform, Ansible, and CloudFormation allow infrastructure to be versioned, consistent, and easily repeatable.

The provided software and enterprise architecture guidelines establish a strong, maintainable foundation for application development. However, a 0DTE (Zero Days to Expiration) SPX trading system operates under strict latency, state, and risk constraints that require adapting some of these general enterprise rules.

## Evaluation of General Best Practices

⚬ What Translates Perfectly:
  ⚬ Event-Driven Architecture & CQRS: Financial markets are inherently event-driven. Treating market data ticks, order updates, and execution fills as asynchronous events, while separating trading commands (routing an order) from state queries (checking account balances), perfectly models trading infrastructure.
  ⚬ Hexagonal Architecture (Ports and Adapters): By separating your core business logic (your 0DTE strategy) from external systems, you can hot-swap simulated paper-trading modules, backtesting engines, and live broker APIs (e.g., Interactive Brokers vs. Tradier) without rewriting the core algorithm.
  ⚬ SOLID, DRY, and KISS: Keeping components decoupled to ensure a class has a single responsibility (SOLID) and avoiding over-engineered complexity (KISS, YAGNI) prevents spaghetti-code bugs that could cause catastrophic financial loss in live markets.
⚬ What Requires Adaptation for Trading:
  ⚬ The Twelve-Factor App (Statelessness): The twelve-factor methodology advocates for stateless processes where persistence is handled by an external database. However, a 0DTE execution engine cannot afford network round-trips to a database to check position sizing during a volatility spike. It must maintain an authoritative, low-latency in-memory state of the order book and risk limits, synchronized asynchronously with write-ahead logs.
  ⚬ Microservices Architecture: While dividing an application into small, independently deployable services improves fault isolation, the network hops (IPC) between microservices introduce latency. For 0DTE SPX, where gamma risk requires sub-second reaction times to market moves, a modular monolith or a tightly coupled in-memory event bus generally outperforms a distributed microservices web.

## Where Argus departs from the rules above, and why

The rules above are the standard the design was held to. Three of them the platform
departs from on purpose, each by a decision record; read the record before "fixing" the
departure.

1. **The circuit breaker halts entries; nothing liquidates.** Rule 3 asks the daily
   max-loss breaker to fire a kill switch that liquidates every open position. Argus's
   breaker and its kill switch stop NEW risk only: the pre-trade gate, the pre-submit
   re-check, and an entry walk already working at the exchange. Exits stay with the
   watchdog, which closes a position on its own triggers (the strike-touch stop, the
   15:30 flatten) through a priced limit walk, never a market order. Cancelling or
   forcing the order that closes a position at the moment the operator presses the
   button would leave them holding exactly the risk they pressed it to shed, and a
   forced liquidation is the unbounded market order rule 1 forbids.
   → [kill-switch scope](../decisions/2026-08-03-kill-switch-scope.md),
   [the daily loss breaker](../decisions/2026-08-20-daily-loss-breaker-restart-rehydration.md),
   [chunk 4: the watchdog and the exit](../decisions/execution-chunk-4-watchdog-exit-notifier.md)

2. **Size is bounded in dollars of worst-case loss, not in Greeks.** Rule 3 asks for
   real-time portfolio delta and gamma caps. Argus sizes a defined-risk vertical by
   what it can lose: `max_loss_per_position`, `max_contracts_per_position`,
   `position_size_pct` of net liquidating value, a concurrency cap welded to
   `daily_max_loss`, and the day's remaining loss budget. For a book of one or two
   5-point verticals those bound the worst case exactly, where a Greek cap would be an
   estimate off a quote that can be stale. The skew/IV regime measurement exists and is
   recorded, and is deliberately inert: telemetry, uncalibrated, never a gate.
   → [chunk 5: the size rails](../decisions/execution-chunk-5-wiring.md),
   [the loss budget](../decisions/2026-09-21-loss-budget-sizing.md),
   [regime telemetry](../decisions/regime-telemetry-and-event-blackout.md)

3. **Idempotency is a question to the broker, not a client order id the broker
   enforces.** Rule 2 asks for a broker-recognised unique client order id. tastytrade
   accepts the same `external_identifier` twice (measured in the sandbox, 2026-07-29),
   so there is no broker-side uniqueness to lean on. Argus's order key is deterministic
   per trade idea (the same candidate rebuilds to the same identifier), and the
   duplicate guard asks the broker's live-order list before any brand-new idea's first
   submission, never local state, because the case it exists for is the one where local
   state was lost. A crash mid-send is recovered by asking the broker what happened and
   halting on anything unexplained, never by resending.
   → [chunk 2: the duplicate guard](../decisions/execution-chunk-2-recording-fills-guard.md),
   [an empty broker answer is not a verdict](../decisions/2026-08-21-empty-broker-answer-is-not-a-verdict.md)

The rest holds as written, with two notes. Rule 1's adaptive limit order is the
price-walking loop (chunk 3), whose cancel/replace is rule 1's "smart cancel/replace";
its parameters are config, and the auction timing is never a literal. Rule 4's
backtest fidelity: the history replay filled at the mid and then applied the fill costs
measured on live orders, and reported both (the public tree's `RESULTS.md`; the
research record itself is private).
