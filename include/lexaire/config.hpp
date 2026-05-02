#pragma once

// Minimal YAML config loader for Lexaire C++ services.
//
// Uses yaml-cpp under the hood. Provides dotted-path access to nested values
// so C++ services can pull whatever subtree they need without threading a
// whole struct through.

#include <filesystem>
#include <stdexcept>
#include <string>
#include <utility>

#include <yaml-cpp/yaml.h>

namespace lexaire {

class Config {
public:
    static Config load(const std::string& override_path = "") {
        std::filesystem::path p = resolve_path(override_path);
        YAML::Node root = YAML::LoadFile(p.string());
        return Config{root, p};
    }

    // Dotted-path getters. Throw if the key is missing and no default is given.
    template <typename T>
    T require(const std::string& dotted) const {
        YAML::Node n = traverse(dotted);
        if (!n || !n.IsDefined())
            throw std::runtime_error("config missing: " + dotted + " (from " + path_.string() + ")");
        try {
            return n.as<T>();
        } catch (const YAML::Exception& e) {
            throw std::runtime_error("config type error at " + dotted + ": " + e.what());
        }
    }

    template <typename T>
    T get_or(const std::string& dotted, T fallback) const {
        YAML::Node n = traverse(dotted);
        if (!n || !n.IsDefined()) return fallback;
        try {
            return n.as<T>();
        } catch (...) {
            return fallback;
        }
    }

private:
    Config(YAML::Node r, std::filesystem::path p) : root_(std::move(r)), path_(std::move(p)) {}

    static std::filesystem::path resolve_path(const std::string& override_path) {
        namespace fs = std::filesystem;
        if (!override_path.empty()) {
            if (fs::is_regular_file(override_path)) return override_path;
            throw std::runtime_error("config not found: " + override_path);
        }
        fs::path cur = fs::current_path();
        for (int i = 0; i < 6; ++i) {
            fs::path candidate = cur / "common" / "config.yaml";
            if (fs::is_regular_file(candidate)) return candidate;
            if (cur.parent_path() == cur) break;
            cur = cur.parent_path();
        }
        throw std::runtime_error("common/config.yaml not found from " + fs::current_path().string());
    }

    YAML::Node traverse(const std::string& dotted) const {
        // YAML::Node has reference semantics — re-binding `cur` to a
        // child doesn't mutate root_. The previous YAML::Clone(root_)
        // deep-copied the entire tree on every traverse() (i.e. on
        // every require<T>() / get_or<T>()), pure waste.
        YAML::Node cur = root_;
        std::size_t start = 0;
        while (start <= dotted.size()) {
            std::size_t dot = dotted.find('.', start);
            std::string part = dotted.substr(start, dot == std::string::npos ? std::string::npos : dot - start);
            if (!cur || !cur.IsDefined() || !cur.IsMap()) return YAML::Node();
            cur = cur[part];
            if (dot == std::string::npos) break;
            start = dot + 1;
        }
        return cur;
    }

    YAML::Node root_;
    std::filesystem::path path_;
};

}  // namespace lexaire
