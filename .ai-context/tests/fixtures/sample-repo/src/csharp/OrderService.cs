using System;

namespace MyApp.Services
{
    // Fixture only — exercises tree-sitter chunk boundaries for C#
    // (_chunk_by_treesitter) and the dotted-namespace capture in _CS_TOP.
    public class OrderService
    {
        private readonly IOrderRepository _repository;

        public OrderService(IOrderRepository repository)
        {
            _repository = repository;
        }

        public Order? FindById(string id)
        {
            return _repository.FindById(id);
        }

        public Order CreateOrder(string customerId, decimal amount)
        {
            var order = new Order(GenerateId(), customerId, amount);
            _repository.Save(order);
            return order;
        }

        private string GenerateId()
        {
            return $"order-{DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()}";
        }
    }

    public interface IOrderRepository
    {
        Order? FindById(string id);
        void Save(Order order);
    }

    public record Order(string Id, string CustomerId, decimal Amount);
}
