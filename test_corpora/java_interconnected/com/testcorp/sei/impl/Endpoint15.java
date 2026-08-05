package com.testcorp.sei.impl;

import javax.jws.WebMethod;
import javax.jws.WebService;
import com.testcorp.api.Handler;
import com.testcorp.api.HandlerImpl3;
import com.testcorp.manager.Manager5;
import com.testcorp.facade.Facade7;
import com.testcorp.service.Service15;

@WebService
public class Endpoint15 {

    @WebMethod
    public String lookup(String id) throws Exception {
        return new Service15().handle(id);
    }

    @WebMethod
    public String dispatch(String id) throws Exception {
        Handler h = new HandlerImpl3();
        return h.run(id);
    }

    @WebMethod
    public String viaManager(String id) throws Exception {
        return new Manager5().process(id);
    }

    @WebMethod
    public String deepChain(String id) throws Exception {
        return new Manager5().deep(id);
    }

    @WebMethod
    public String deepest(String id) throws Exception {
        return new Facade7().orchestrate(id);
    }

    @WebMethod
    public String viaFacade(String id) throws Exception {
        return new Facade7().orchestrateDirect(id);
    }

    @WebMethod
    public String viaLambda(String id) throws Exception {
        final Service15 s = new Service15();
        java.util.List<String> one = java.util.Collections.singletonList(id);
        final StringBuilder sb = new StringBuilder();
        one.forEach(v -> {
            try {
                sb.append(s.handle(v));
            } catch (Exception e) {
                sb.append("");
            }
        });
        return sb.toString();
    }

    @WebMethod
    public String viaAnon(final String id) throws Exception {
        Handler h = new Handler() {
            @Override
            public String run(String input) throws Exception {
                return new Service15().handleTraced(input);
            }
        };
        return h.run(id);
    }
}
