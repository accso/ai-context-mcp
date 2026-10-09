package com.example.fixture;

import java.util.Optional;

/**
 * Fixture only — exercises tree-sitter chunk boundaries for Java
 * (_chunk_by_treesitter, via symbol_extractor's tree-sitter parser).
 */
public class OrderService {

    private final OrderRepository repository;

    public OrderService(OrderRepository repository) {
        this.repository = repository;
    }

    public Optional<Order> findById(String id) {
        return repository.findById(id);
    }

    public Order createOrder(String customerId, double amount) {
        Order order = new Order(generateId(), customerId, amount);
        repository.save(order);
        return order;
    }

    private String generateId() {
        return "order-" + System.currentTimeMillis();
    }
}

interface OrderRepository {
    Optional<Order> findById(String id);
    void save(Order order);
}

record Order(String id, String customerId, double amount) {
}
