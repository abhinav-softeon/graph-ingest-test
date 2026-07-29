package com.acme.web;

import com.acme.zoo.Zoo;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class AnimalController {
    private final Zoo zoo = new Zoo();

    @GetMapping("/animals/announce")
    public String announce() {
        zoo.announceAll();
        return "ok";
    }
}
