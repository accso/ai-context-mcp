<!--
  Fixture only — exercises Vue SFC block splitting (_chunk_vue) and the
  still-open B.10 roadmap question: does the extracted <script setup> block
  get further tree-sitter/regex sub-chunked, or stay one opaque chunk?
-->
<template>
  <div class="user-card">
    <h2>{{ user.name }}</h2>
    <p>{{ user.email }}</p>
    <button @click="toggleExpanded">{{ expanded ? "Collapse" : "Expand" }}</button>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from "vue";

const props = defineProps<{ user: { name: string; email: string } }>();
const expanded = ref(false);

const displayName = computed(() =>
  expanded.value ? `${props.user.name} <${props.user.email}>` : props.user.name
);

function toggleExpanded(): void {
  expanded.value = !expanded.value;
}
</script>

<style scoped>
.user-card {
  border: 1px solid #ccc;
  padding: 1rem;
}
</style>
