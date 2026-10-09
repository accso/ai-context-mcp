package com.example.fixture

/**
 * Fixture only — exercises regex-fallback chunk boundaries (_KT_TOP in
 * chunker.py), since Kotlin has no tree-sitter chunk-boundary path yet.
 */
class UserService(private val repository: UserRepository) {

    fun findById(id: String): User? {
        return repository.findById(id)
    }

    fun createUser(name: String, email: String): User {
        val user = User(id = generateId(), name = name, email = email)
        repository.save(user)
        return user
    }

    private fun generateId(): String {
        return "user-${System.currentTimeMillis()}"
    }
}

data class User(val id: String, val name: String, val email: String)

interface UserRepository {
    fun findById(id: String): User?
    fun save(user: User)
}
